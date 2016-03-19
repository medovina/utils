#!/usr/bin/python3

import sys

'''
Prints ReCodEx backend evaluate chain for one testing case.
TODO: in_type == "dir" not yet supported.
'''


def print_task(identity, priority, fatal, dependencies, binary, args, output):
    output.write('    - task-id: "{}"\n'.format(identity))
    output.write('      priority: {}\n'.format(priority))
    if fatal:
        output.write('      fatal-failure: true\n')
    else:
        output.write('      fatal-failure: false\n')

    if dependencies:
        output.write('      dependencies:\n')
        for dep in dependencies:
            output.write('          - {}\n'.format(dep))

    output.write('      cmd:\n')
    output.write('          bin: "{}"\n'.format(binary))

    if args:
        output.write('          args:\n')
        for arg in args:
            output.write('              - "{}"\n'.format(arg))


def print_sandbox(test, ext, output=sys.stdout, judge=False):
    output.write('      sandbox:\n')
    output.write('          name: "isolate"\n')
    output.write('          limits:\n')
    output.write('              - hw-group-id: group1\n')

    if judge:
        # Set reasonable defaults for judges
        time = '2.0'
        memory = '16384'
    elif ext in test.limits:
        # There are extension specific values
        time = test.limits[ext].time_limit
        memory = test.limits[ext].mem_limit
    else:
        # Use test 'default' values
        time = test.limits['default'].time_limit
        memory = test.limits['default'].mem_limit

    output.write('                time: {}\n'. format(time))
    output.write('                memory: {}\n'.format(memory))
    output.write('                chdir: ${EVAL_DIR}\n')
    output.write('                environ-variable:\n')
    output.write('                    PATH: "/usr/bin"\n')
    output.write('                bound-directories:\n')
    output.write('                    - src: ${SOURCE_DIR}\n')
    output.write('                      dst: ${EVAL_DIR}\n')
    output.write('                      mode: RW\n')


def print_one_test(test, ext, output=sys.stdout):
    priority = 1

    if not test.in_file:
        test.in_file = "{}.stdin".format(test.number)
    if not test.out_file:
        test.out_file = "{}.stdout".format(test.number)

    # Fetch input
    args = ["{}.in".format(test.number), "${{SOURCE_DIR}}/{}".format(test.in_file)]
    fetch_input = "fetch_input_{}".format(test.number)
    print_task(fetch_input, priority, False, None, "fetch", args, output)
    priority += 1

    # Evaluate test
    eval_task = "eval_task_{}".format(test.number)
    print_task(eval_task, priority, False, [fetch_input], "a.out", None, output)
    if test.in_type == "stdio":
        output.write('      stdin: {}\n'.format(test.in_file))
    if test.out_type == "stdio":
        output.write('      stdout: {}\n'.format(test.out_file))
    print_sandbox(test, ext, output)
    priority += 1

    # Fetch sample output
    args = ["{}.out".format(test.number), "${{SOURCE_DIR}}/{}.out".format(test.number)]
    fetch_output = "fetch_output_{}".format(test.number)
    print_task(fetch_output, priority, False, [eval_task], "fetch", args, output)
    priority += 1

    # Filter outputs (clean comments)
    args = [test.out_file, "{}_filtered".format(test.out_file)]
    judge_filter = "judge_filter_{}".format(test.number)
    print_task(judge_filter, priority, False, [fetch_output], "${JUDGES_DIR}/recodex-judge-filter", args, output)
    print_sandbox(test, ext, output, judge=True)
    priority += 1

    # Judging results
    args = ["{}.out".format(test.number), "{}_filtered".format(test.out_file)]
    judge_results = "judge_test_{}".format(test.number)
    print_task(judge_results, priority, False, [judge_filter], "${JUDGES_DIR}/recodex-judge-normal", args, output)
    print_sandbox(test, ext, output, judge=True)
    priority += 1

    # Remove junk
    args = ["${{SOURCE_DIR}}/{}.out".format(test.number), "${{SOURCE_DIR}}/{}".format(test.in_file),
            "${{SOURCE_DIR}}/{}".format(test.out_file), "${{SOURCE_DIR}}/{}_filtered".format(test.out_file)]
    remove_junk = "remove_junk_{}".format(test.number)
    print_task(remove_junk, priority, False, [judge_results], "rm", args, output)
