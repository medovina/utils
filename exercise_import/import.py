import click
import requests
import re
import logging
import yaml
from bs4 import BeautifulSoup
from pathlib import Path
from html2text import html2text
from functools import partial
from collections import defaultdict

from .codex_config import load_codex_test_config
from .api import ApiClient

class Config:
    def __init__(self, api_url, api_token, locale, extension_to_runtime, extension_to_pipeline):
        self.api_url = api_url
        self.api_token = api_token
        self.locale = locale
        self.extension_to_runtime = extension_to_runtime
        self.extension_to_pipeline = extension_to_pipeline

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
                    "exec": {
                        "files": "Mono execution & evaluation",
                        "stdio": "Mono stdin/out execution & evaluation"
                    }
                },
                "c": {
                    "build": "GCC Compilation",
                    "exec": {
                        "files": "ELF execution & evaluation",
                        "stdio": "ELF stdin/out execution & evaluation"
                    }
                },
                "pas": {
                    "build": "FreePascal Compilation",
                    "exec": {
                        "files": "ELF execution & evaluation",
                        "stdio": "ELF stdin/out execution & evaluation"
                    }
                },
                "java": {
                    "build": "Javac Compilation",
                    "exec": {
                        "files": "Java execution & evaluation",
                        "stdio": "Java stdin/out execution & evaluation"
                    }
                },
                "cpp": {
                    "build": "G++ Compilation",
                    "exec": {
                        "files": "ELF execution & evaluation",
                        "stdio": "ELF stdin/out execution & evaluation"
                    }
                }
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
    result["description"] = soup.select("data comment")[0].get_text()
    result["difficulty"] = "easy"
    result["isPublic"] = True
    result["isLocked"] = True

    return result

def load_active_text(soup):
    text_entry = soup.select("text[active=1]")[0]
    content = text_entry.find("content").get_text()

    return text_entry["id"], html2text(content)

def load_additional_files(exercise_folder, text_id):
    path = Path(exercise_folder) / "texts" / text_id
    return list(path.glob("*"))

def replace_file_references(text, url_map):
    """
    >>> replace_file_references("[link]($DIR/foo.zip)", {"foo.zip": "https://my.web.com/archive.zip"})
    '[link](https://my.web.com/archive.zip)'
    >>> replace_file_references("![kitten]($DIR/foo.jpg)", {"foo.jpg": "https://my.web.com/image.jpg"})
    '![kitten](https://my.web.com/image.jpg)'
    """

    def replace(match):
        filename = match.group(1)
        return "({})".format(url_map.get(filename, ""))

    return re.sub(r'\(\$DIR/(.*)\)', replace, text)


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
        if file_node.name == "config":
            continue
        if file_node.suffix in (".in", ".out") and file_node.is_dir():
            for child in file_node.iterdir():
                yield "{}.{}".format(file_node.stem, child.name), child
        else:
            yield file_node.name, file_node

def load_allowed_extensions(content_soup):
    for item in content_soup.select("extensions item"):
        yield item.get_text()

def make_exercise_config(config, content_soup, exercise_file_id_map, pipelines, tests):
    extensions = list(load_allowed_extensions(content_soup))

    exercise_config = []
    exercise_config.append({
        "name": "default",
        "tests": [{"name": test.name, "pipelines": []} for test in tests]
    })

    pipeline_map = {item["name"]: item["id"] for item in pipelines}
    input_files = {name: file_id for name, file_id in exercise_file_id_map.items() if name.endwsith(".in")}

    for extension in load_allowed_extensions(content_soup):
        environment = config.extension_to_runtime[extension]
        env_tests = []

        for test in tests:
            build_pipeline = pipeline_map[config.extension_to_pipeline[extension]["build"]]
            exec_pipelines = pipeline_map[config.extension_to_pipeline[extension]["exec"]]

            test_stdio = test.in_type == "stdio"
            exec_pipeline = exec_pipelines["stdio"] if test_stdio else exec_pipelines["files"]

            if not test_stdio:
                relevant_inputs = {
                    name: file_id for name, file_id in input_files.items()
                    if name.startswith("{}.".format(test.number))
                } 
                test_inputs = list(relevant_inputs.values())
                test_input_names = list(relevant_inputs.keys())
            else:
                test_inputs = exercise_file_id_map["{}.in".format(test.number)]

            env_tests.append({
                "name": test.name,
                "pipelines": [
                    {
                        "name": build_pipeline,
                        "variables": []
                    },
                    {
                        "name": exec_pipeline,
                        "variables": [
                            {
                                "name": "input-files" if not test_stdio else "input-file",
                                "type": "remote-file[]" if not test_stdio else "remote_file",
                                "value": test_inputs
                            },
                            {
                                "name": "actual-inputs" if not test_stdio else "actual-input",
                                "type": "file[]" if not test_stdio else "file",
                                "value": test_input_names
                            },
                            {
                                "name": "expected-output",
                                "type": "remote-file",
                                "value": exercise_file_id_map["{}.out".format(test.number)]
                            },
                            {
                                "name": "actual-output",
                                "type": "file",
                                "value": test.out_file
                            }
                        ]
                    }
                ]
            })

    return exercise_config

def upload_file(api, path, filename=None):
    filename = filename or path.name
    logging.info("Uploading {}".format(filename) if filename is None else "Uploading {} as {}".format(path.name, filename))

    payload = api.upload_file(filename, path.open("rb"))
    uploaded_file_id = payload["id"]

    logging.info("Uploaded with id %s", uploaded_file_id)

    return uploaded_file_id

def check_for_cross_io_tests(tests):
    for test in tests:
        if (test.in_type == "stdio") != (test.out_type == "stdio"):
            return True

    return False

def check_for_strange_judges(tests):
    for test in tests:
        if test.judge != "bin/codex-judge":
            return test.judge

    return None

@click.group()
def cli():
    pass

@cli.command()
@click.argument("exercise_folder")
def details(exercise_folder):
    soup = load_content(exercise_folder)

    print("### Exercise details")
    print(load_details(soup))
    print()

    print("### Exercise assignment text")
    print(load_active_text(soup))
    print()

    config = Config.load(Path.cwd() / "import-config.yml")
    api = ApiClient(config.api_url, config.api_token)
    tests = load_codex_test_config(Path(exercise_folder) / "testdata" / "config")

    print("### Exercise configuration")
    print(make_exercise_config(config, soup, defaultdict(lambda: "random-file-uuid"), api.get_pipelines(), tests))
    print()

@cli.command()
@click.argument("exercise_folder")
@click.argument("group_id")
def run(exercise_folder, group_id):
    logging.basicConfig(level=logging.INFO)

    config = Config.load(Path.cwd() / "import-config.yml")
    logging.info("Configuration loaded")

    api = ApiClient(config.api_url, config.api_token)

    content_soup = load_content(exercise_folder)
    logging.info("content.xml loaded")

    # Create a new, empty exercise
    creation_payload = api.create_exercise(group_id)
    exercise_id = creation_payload["id"]
    logging.info("Exercise created with id %s", exercise_id)

    # Upload additional files (attachments) and associate them with the exercise
    text_id, text = load_active_text(content_soup)
    id_map = {}

    logging.info("Uploading attachments")
    for path in load_additional_files(exercise_folder, text_id):
        id_map[path.name] = upload_file(api, path)

    if id_map:
        api.add_exercise_attachments(exercise_id, id_map)

    logging.info("Uploaded attachments associated with the exercise")

    # Prepare the exercise text
    url_map = {filename: "{}/v1/uploaded-files/{}/download".format(config.api_url, file_id) for filename, file_id in id_map.items()}
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
    exercise_file_id_map = {}

    logging.info("Uploading supplementary exercise files")
    for name, path in load_exercise_files(exercise_folder):
        exercise_file_id_map[name] = upload_file(api, path, name)

    api.add_exercise_files(exercise_id, exercise_file_id_map)
    logging.info("Uploaded exercise files associated with the exercise")

    # Upload reference solutions
    for solution_id, solution in load_reference_solution_details(content_soup, config.extension_to_runtime):
        path = load_reference_solution_file(solution_id, content_soup, exercise_folder)
        solution["files"] = [upload_file(api, path)]
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
    if check_for_cross_io_tests(tests):
        logging.warning("Exercise %s takes input from stdin and writes a file (or vice-versa)", exercise_id)

    strange_judge = check_for_strange_juddges(tests)
    if strange_judge is not None:
        logging.warning("Exercise %s uses a non-default judge %s", exercise_id, strange_judge)

    exercise_config = make_exercise_config(
        config,
        content_soup,
        exercise_file_id_map,
        api.get_pipelines(),
        tests
    )

    api.update_exercise_config(exercise_id, exercise_config)
    logging.info("Exercise config updated")


if __name__ == '__main__':
    cli()

