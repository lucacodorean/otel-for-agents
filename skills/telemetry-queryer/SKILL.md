---
name: telemetry-queryer
description: Query the telemetry data collected through from the claude code sessions
disable-model-invocation: true
argument-hint: "<question> --formatted"
allowed-tools:
  - Bash(bash ${CLAUDE_PROJECT_DIR}/infra/start.sh)
  - Bash(bash ${CLAUDE_PROJECT_DIR}/infra/stop.sh)
---

## Task queryer

Provide structured telemetry data from the telemetry API. Interactions are made through Prometheus query language (PromQL) queries. The telemetry API is a Prometheus-compatible API that collects and exposes metrics from the agentic environment.


## Invocations and parameters

Invocation takes place through '/talemetry-queryer' command. The arguments of the skill are presented below:

| Argument | Type | Description | Default |
|----------|------|-------------|---------|
| question (required) | string | The question to query the telemetry data with | N/A |
| --formatted (optional) | flag | Formatted output according to 'references/formatted_output.md' | False |

## Mode of operation 

### Parsing arguments

The arguments are passed as command-line options, meaning that the flages are prefixed by the their argument. Each argument is parsed. In case arguments are not provided, the default values are used. In case flags that are not in the parameters lists are provided, the skill informs the client through the the format provided at 'references/invalid_request.md'. The question is received through the 'question' argument, while the rest of the arguments are parsed as flags. The skill will then parse the question to obtain relevant information for the PromQL query. If the question is not clear or cannot be parsed, the skill will inform the client through the format provided at 'references/invalid_request.md'. Refer to 'references/invalid_request.md' for the output format of invalid requests.

### Querying the telemetry API

The parsed question is then used to form the PromQL query. The skill will then query the telemetry API using PromQL queries to retrieve the relevant data. If the query is invalid or cannot be executed, the skill will inform the client through the format provided at 'references/invalid_request.md'. Otherwise, output will be formatted according to 'references/formatted_output.md' if the '--formatted' flag is set to true.

### Handling Prometheus server availability

If the Prometheus server is offline, the skill should call the ./infra/start.sh script to start the Prometheus server.  If the Prometheus server is not reachable after starting it, the skill should inform the client that the server is unreachable using the format provided at 'references/invalid_request.md'. If the Prometheus server is already running, the skill should not call the ./infra/start.sh script. If the Prometheus server is running but not reachable, the skill should inform the client that the server is unreachable using the format provided at 'references/invalid_request.md'.

If the Prometheus server was started as an effect of the given run, also run ./infra/stop.sh to stop it.

### Responses

Responses should be made based on the format provided at 'references/formatted_output.md' if the '--formatted' flag is set to true. Otherwise, responses should be made in natural language. In case of errors, responses should be made based on the format provided at 'references/invalid_request.md'. Do not provide any other text in the response besides the given formats. 