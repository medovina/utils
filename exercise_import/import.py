import click
import re
import logging
import yaml
import sys
from bs4 import BeautifulSoup
from pathlib import Path
from html2text import html2text
from collections import defaultdict
from pprint import pprint

from .codex_config import load_codex_test_config
from .api import ApiClient

class Config:
    def __init__(self, api_url, api_token, locale, extension_to_runtime, extension_to_pipeline, judges):
        self.api_url = api_url
        self.api_token = api_token
        self.locale = locale
        self.extension_to_runtime = extension_to_runtime
        self.extension_to_pipeline = extension_to_pipeline
        self.judges = judges

    @classmethod
    def load(cls, config_path):
        config = {
            "extension_to_runtime": {
                "cs": "mono46",
                "c": "c-gcc-linux",
                "pas": "freepascal-linux",
                "java": "java8",
                "cpp": "cxx11-gcc-linux"
            },
            "extension_to_pipeline": {
                "cs": {
                    "build": "MCS Compilation",
                    "exec": "Mono {input}-{output} execution & evaluation"
                },
                "c": {
                    "build": "GCC Compilation",
                    "exec": "ELF {input}-{output} execution & evaluation"
                },
                "pas": {
                    "build": "FreePascal Compilation",
                    "exec": "ELF {input}-{output} execution & evaluation"
                },
                "java": {
                    "build": "Javac Compilation",
                    "exec": "Java {input}-{output} execution & evaluation"
                },
                "cpp": {
                    "build": "G++ Compilation",
                    "exec": "ELF {input}-{output} execution & evaluation"
                },
                "py": {
                    "build": "Python pass-through compilation",
                    "exec": "Python {input}-{output} execution & evaluation"
                }

            },
            "judges": {
                "bin/codex_judge": "recodex-judge-normal",
                "bin/codex_shufflejudge": "recodex-judge-shuffle",
                "diff": "diff"
            },
            "locale": "cs"
        }

        data = yaml.load(config_path.open("r"))
        config.update(data)

        return cls(**config)

def load_content(exercise_folder):
    content = (Path(exercise_folder) / "content.xml").read_bytes()
    return BeautifulSoup(content, "lxml")

def load_details(soup):
    result = {}

    result["name"] = soup.select("data name")[0].get_text()
    result["version"] = soup.find("exercise")["version"]
    result["description"] = soup.select("data comment")[0].get_text() or "Lorem ipsum"
    result["difficulty"] = "easy"
    result["isPublic"] = True
    result["isLocked"] = True

    return result

def load_active_text(soup):
    text_entry = soup.select("text[active=1]")[0]
    content = text_entry.find("content").get_text()
    content = BeautifulSoup(content, "lxml")

    for node in content.select("code a"):
        node.parent.unwrap()

    return text_entry["id"], html2text(str(content))

def load_additional_files(exercise_folder, text_id):
    path = Path(exercise_folder) / "texts" / text_id
    return list(path.glob("*"))

def replace_file_references(text, url_map):
    """
    >>> replace_file_references("[link]($DIR/foo.zip)", {"foo.zip": "https://my.web.com/archive.zip"})
    '[link](https://my.web.com/archive.zip)'
    >>> replace_file_references("![kitten]($DIR/foo.jpg)", {"foo.jpg": "https://my.web.com/image.jpg"})
    '![kitten](https://my.web.com/image.jpg)'
    >>> replace_file_references("(see ![kitten]($DIR/foo.jpg))", {"foo.jpg": "https://my.web.com/image.jpg"})
    '(see ![kitten](https://my.web.com/image.jpg))'
    """

    def replace(match):
        filename = match.group(1)
        return "({})".format(url_map.get(filename, ""))

    return re.sub(r'\(\$DIR/(.*?)\)', replace, text)


def load_reference_solution_details(content_soup, extension_to_runtime):
    for solution in content_soup.select("solution"):
        yield solution["id"], {
            "note": solution.find("comment").get_text(),
            "runtimeEnvironmentId": extension_to_runtime[solution.find("extension").get_text()]
        }

def load_reference_solution_file(solution_id, content_soup, exercise_folder):
    extension = content_soup.select("solution[id={}] extension".format(solution_id))[0].get_text()
    return Path(exercise_folder) / "solutions" / solution_id / "source.{}".format(extension)

def load_exercise_files(exercise_folder):
    path = Path(exercise_folder) / "testdata"
    for file_node in path.iterdir():
        if file_node.name == "config" or (file_node.is_dir() and file_node.name == "attic"):
            continue
        if file_node.suffix in (".in", ".out") and file_node.is_dir():
            for child in file_node.iterdir():
                yield "{}.{}".format(file_node.stem, child.name), child
        else:
            yield file_node.name, file_node

def load_allowed_extensions(content_soup):
    for item in content_soup.select("extensions item"):
        yield item.get_text()

def make_exercise_config(config, content_soup, exercise_file_data, pipelines, tests):
    exercise_config = []
    exercise_config.append({
        "name": "default",
        "tests": [{"name": test.name, "pipelines": []} for test in tests]
    })

    pipeline_map = {item["name"]: item["id"] for item in pipelines}
    input_files = {name: hashName for name, hashName in exercise_file_data.items() if name.endswith(".in")}

    for extension in load_allowed_extensions(content_soup):
        environment = config.extension_to_runtime[extension]
        env_tests = []

        for test in tests:
            build_pipeline = pipeline_map[config.extension_to_pipeline[extension]["build"]]
            input_stdio = test.in_type == "stdio"
            output_stdio = test.out_type == "stdio"

            try:
                exec_pipeline_name = config.extension_to_pipeline[extension]["exec"].format(
                    input="stdin" if input_stdio else "files",
                    output="stdout" if output_stdio else "file"
                )
                exec_pipeline = pipeline_map[exec_pipeline_name]
            except KeyError:
                logging.error(
                    "Pipeline '%s' was not found, aborting configuration of test %s (environment %s)",
                    pipeline_name, test.name, environment
                )
                continue

            if not input_stdio:
                relevant_inputs = {
                    name: hashName for name, hashName in input_files.items()
                    if name.startswith("{}.".format(test.number))
                } 
                test_inputs = list(relevant_inputs.values())
                test_input_names = [test.in_file] if test.in_type == "file" else [name[name.index(".") + 1 : ] for name in relevant_inputs.keys()]
            else:
                test_inputs = input_files["{}.in".format(test.number)]
                test_input_names = None

            variables = [
                {
                    "name": "input-files" if not input_stdio else "input-file",
                    "type": "remote-file[]" if not input_stdio else "remote-file",
                    "value": test_inputs
                },
                {
                    "name": "expected-output",
                    "type": "remote-file",
                    "value": exercise_file_data["{}.out".format(test.number)]
                },
                {
                    "name": "judge-type",
                    "type": "string",
                    "value": config.judges.get(test.judge, test.judge)
                },
                {
                    "name": "run-args",
                    "type": "string[]",
                    "value": convert_args(test)
                }
            ]

            if not input_stdio:
                variables.append({
                    "name": "actual-inputs",
                    "type": "file[]",
                    "value": test_input_names
                })

            if not output_stdio:
                variables.append({
                    "name": "actual-output",
                    "type": "file",
                    "value": test.out_file or "{}.actual.out".format(test.number)
                })

            env_tests.append({
                "name": test.name,
                "pipelines": [
                    {
                        "name": build_pipeline,
                        "variables": []
                    },
                    {
                        "name": exec_pipeline,
                        "variables": variables
                    }
                ]
            })

        exercise_config.append({
            "name": environment,
            "tests": env_tests
        })

    return exercise_config

def upload_file(api, path, filename=None):
    filename = filename or path.name
    logging.info("Uploading {}".format(filename) if filename is None else "Uploading {} as {}".format(path.name, filename))

    payload = api.upload_file(filename, path.open("rb"))

    logging.info("Uploaded with id %s", payload["id"])

    return payload

def check_for_strange_judges(config, tests):
    for test in tests:
        if test.judge not in config.judges.keys():
            return test.judge

    return None

def convert_args(test):
    if "./$PROBLEM" not in test.cmd_args:
        return []

    program_index = test.cmd_args.index("./$PROBLEM")
    return test.cmd_args[program_index + 1 :]

@click.group()
def cli():
    pass

@cli.command()
@click.argument("exercise_folder")
def details(exercise_folder):
    soup = load_content(exercise_folder)

    print("### Exercise details")
    pprint(load_details(soup))
    print()

    print("### Exercise assignment text")
    pprint(load_active_text(soup))
    print()

    config = Config.load(Path.cwd() / "import-config.yml")
    api = ApiClient(config.api_url, config.api_token)
    tests = load_codex_test_config(Path(exercise_folder) / "testdata" / "config")
    files = defaultdict(lambda: "random-file-uuid")

    for name, path in load_exercise_files(exercise_folder):
        files.get(name) # Make sure the keys are present in the exercise file map

    print("### Exercise configuration")
    pprint(make_exercise_config(config, soup, files, api.get_pipelines(), tests))
    print()

@cli.command()
@click.argument("exercise_folder")
def name(exercise_folder):
    content_soup = load_content(exercise_folder)
    details = load_details(content_soup)
    print(details["name"])

@cli.command()
@click.argument("exercise_folder", nargs=-1)
@click.option("config_path", "-c")
def get_id(exercise_folder, config_path=None):
    config = Config.load(Path.cwd() / (config_path or "import-config.yml"))
    api = ApiClient(config.api_url, config.api_token)
    exercises = api.get_exercises()

    for folder in exercise_folder:
        found = False
        content_soup = load_content(folder)
        details = load_details(content_soup)
        for exercise in exercises:
            if exercise["name"] == details["name"]:
                print(folder, exercise["id"])
                found = True
                break

        if not found:
            print(folder, "Nothing found")

@cli.command()
@click.argument("language")
@click.argument("exercise_id")
@click.option("config_path", "-c")
def add_localization(language, exercise_id, config_path):
    config = Config.load(Path.cwd() / (config_path or "import-config.yml"))
    api = ApiClient(config.api_url, config.api_token)

    exercise = api.get_exercise(exercise_id)
    exercise["localizedTexts"].append({
        "locale": language,
        "text": html2text(sys.stdin.read())
    })

    api.update_exercise(exercise_id, exercise)

@cli.command()
@click.option("config_path", "-c")
@click.option("exercise_id", "-e")
@click.argument("exercise_folder")
@click.argument("group_id")
def run(exercise_folder, group_id, config_path=None, exercise_id=None):
    logging.basicConfig(level=logging.INFO)

    logging.info("*** Importing from %s", exercise_folder)

    config = Config.load(Path.cwd() / (config_path or "import-config.yml"))
    logging.info("Configuration loaded")

    api = ApiClient(config.api_url, config.api_token)

    content_soup = load_content(exercise_folder)
    logging.info("content.xml loaded")

    # If no exercise ID was given, create a new, empty exercise
    if exercise_id is None:
        creation_payload = api.create_exercise(group_id)
        exercise_id = creation_payload["id"]
        logging.info("Exercise created with id %s", exercise_id)
    else:
        logging.info("Reusing exercise with id %s", exercise_id)

    # Upload additional files (attachments) and associate them with the exercise
    text_id, text = load_active_text(content_soup)
    attachment_ids = set()

    logging.info("Uploading attachments")
    for path in load_additional_files(exercise_folder, text_id):
        attachment_ids.add(upload_file(api, path)["id"])

    if attachment_ids:
        api.add_exercise_attachments(exercise_id, attachment_ids)

    logging.info("Uploaded attachments associated with the exercise")

    # Prepare the exercise text
    attachments = api.get_exercise_attachments(exercise_id)
    url_map = {item["name"]: "{}/v1/uploaded-files/{}/download".format(config.api_url, item["id"]) for item in attachments}
    text = replace_file_references(text, url_map)

    # Set the details of the new exercise
    details = load_details(content_soup)
    details["localizedTexts"] = [{
        "locale": config.locale,
        "text": text
    }]

    api.update_exercise(exercise_id, details)
    logging.info("Exercise details updated")

    # Upload exercise files and associate them with the exercise
    exercise_file_data = {}

    logging.info("Uploading supplementary exercise files")
    for name, path in load_exercise_files(exercise_folder):
        exercise_file_data[name] = upload_file(api, path, name)

    api.add_exercise_files(exercise_id, [data["id"] for data in exercise_file_data.values()])
    logging.info("Uploaded exercise files associated with the exercise")

    # Upload reference solutions
    for solution_id, solution in load_reference_solution_details(content_soup, config.extension_to_runtime):
        path = load_reference_solution_file(solution_id, content_soup, exercise_folder)
        solution["files"] = [upload_file(api, path)["id"]]
        payload = api.create_reference_solution(exercise_id, solution)

        logging.info("New reference solution created, with id %s", payload["id"])

    # Configure environments
    extensions = list(load_allowed_extensions(content_soup))
    environments = [config.extension_to_runtime[ext] for ext in extensions]
    env_data = {item["id"]: item for item in api.get_runtime_environments()}
    env_configs = [
        {
            "runtimeEnvironmentId": env_id,
            "variablesTable": env_data[env_id]["defaultVariables"]
        } for env_id in environments
    ]

    api.update_environment_configs(exercise_id, env_configs)
    logging.info("Added environments %s", ", ".join(environments))

    # Configure tests
    tests = load_codex_test_config(Path(exercise_folder) / "testdata" / "config")

    strange_judge = check_for_strange_judges(config, tests)
    if strange_judge is not None:
        logging.warning("Exercise %s uses a non-default judge %s", exercise_id, strange_judge)

    exercise_config = make_exercise_config(
        config,
        content_soup,
        {item["name"]: item["hashName"] for item in api.get_exercise_files(exercise_id)},
        api.get_pipelines(),
        tests
    )

    api.update_exercise_config(exercise_id, exercise_config)
    logging.info("Exercise config updated")
    
    # Configure test limits
    for extension, environment_id in zip(extensions, environments):
        limits_config = {}

        for test in tests:
            key = extension if extension in test.limits.keys() else "default"
            limits_config[test.name] = {
                    "wall-time": test.limits[key].time_limit,
                    "memory": test.limits[key].mem_limit
            }
        
        api.update_limits(exercise_id, environment_id, limits_config)
        logging.info("Limits set for environment %s", environment_id)

    api.evaluate_reference_solutions(exercise_id)
    logging.info("Requested an evaluation of reference solutions")

if __name__ == '__main__':
    cli()

