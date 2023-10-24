"""
# AWS Lambda system tests

This testsuite uses boto3 to upload actual Lambda functions to AWS Lambda.

For running test locally you need to set these env vars (You can find the values in the Sentry password manager):
export SENTRY_PYTHON_TEST_AWS_ACCESS_KEY_ID="..."
export SENTRY_PYTHON_TEST_AWS_SECRET_ACCESS_KEY="..."

If you need to debug a new runtime, use this REPL to figure things out:

    pip3 install click
    python3 tests/integrations/aws_lambda/client.py --runtime=python4.0
"""
import base64
import json
import re
from textwrap import dedent

import pytest

LAMBDA_PRELUDE = """
from sentry_sdk.integrations.aws_lambda import AwsLambdaIntegration, get_lambda_bootstrap
import sentry_sdk
import json
import time

from sentry_sdk.transport import HttpTransport

def truncate_data(data):
    # AWS Lambda truncates the log output to 4kb, which is small enough to miss
    # parts of even a single error-event/transaction-envelope pair if considered
    # in full, so only grab the data we need.

    cleaned_data = {}

    if data.get("type") is not None:
        cleaned_data["type"] = data["type"]

    if data.get("contexts") is not None:
        cleaned_data["contexts"] = {}

        if data["contexts"].get("trace") is not None:
            cleaned_data["contexts"]["trace"] = data["contexts"].get("trace")

    if data.get("transaction") is not None:
        cleaned_data["transaction"] = data.get("transaction")

    if data.get("request") is not None:
        cleaned_data["request"] = data.get("request")

    if data.get("tags") is not None:
        cleaned_data["tags"] = data.get("tags")

    if data.get("exception") is not None:
        cleaned_data["exception"] = data.get("exception")

        for value in cleaned_data["exception"]["values"]:
            for frame in value.get("stacktrace", {}).get("frames", []):
                del frame["vars"]
                del frame["pre_context"]
                del frame["context_line"]
                del frame["post_context"]

    if data.get("extra") is not None:
        cleaned_data["extra"] = {}

        for key in data["extra"].keys():
            if key == "lambda":
                for lambda_key in data["extra"]["lambda"].keys():
                    if lambda_key in ["function_name"]:
                        cleaned_data["extra"].setdefault("lambda", {})[lambda_key] = data["extra"]["lambda"][lambda_key]
            elif key == "cloudwatch logs":
                for cloudwatch_key in data["extra"]["cloudwatch logs"].keys():
                    if cloudwatch_key in ["url", "log_group", "log_stream"]:
                        cleaned_data["extra"].setdefault("cloudwatch logs", {})[cloudwatch_key] = data["extra"]["cloudwatch logs"][cloudwatch_key]

    if data.get("level") is not None:
        cleaned_data["level"] = data.get("level")

    if data.get("message") is not None:
        cleaned_data["message"] = data.get("message")

    if "contexts" not in cleaned_data:
        raise Exception(json.dumps(data))

    return cleaned_data

def event_processor(event):
    return truncate_data(event)

def envelope_processor(envelope):
    (item,) = envelope.items
    item_json = json.loads(item.get_bytes())

    return truncate_data(item_json)


class TestTransport(HttpTransport):
    def _send_event(self, event):
        event = event_processor(event)
        print("x")  # force AWS lambda logging to start a new line
                    # (when printing a stacktrace it swallows the \\n from the next print statement)
        print("\\nEVENT: {}\\n".format(json.dumps(event)))

    def _send_envelope(self, envelope):
        envelope = envelope_processor(envelope)
        print("x")  # force AWS lambda logging to start a new line
                    # (when printing a stacktrace it swallows the \\n from the next print statement)
        print("\\nENVELOPE: {}\\n".format(json.dumps(envelope)))

def init_sdk(timeout_warning=False, **extra_init_args):
    sentry_sdk.init(
        dsn="https://123abc@example.com/123",
        transport=TestTransport,
        integrations=[AwsLambdaIntegration(timeout_warning=timeout_warning)],
        shutdown_timeout=10,
        **extra_init_args
    )
"""


@pytest.fixture
def lambda_client():
    from tests.integrations.aws_lambda.client import get_boto_client

    return get_boto_client()


@pytest.fixture(
    params=[
        "python3.9",
        "python3.10",
        "python3.11",
    ]
)
def lambda_runtime(request):
    return request.param


@pytest.fixture
def run_lambda_function(request, lambda_client, lambda_runtime):
    def inner(
        code, payload, timeout=30, syntax_check=True, layer=None, initial_handler=None
    ):
        from tests.integrations.aws_lambda.client import run_lambda_function

        response = run_lambda_function(
            client=lambda_client,
            runtime=lambda_runtime,
            code=code,
            payload=payload,
            add_finalizer=request.addfinalizer,
            timeout=timeout,
            syntax_check=syntax_check,
            layer=layer,
            initial_handler=initial_handler,
        )

        # for better debugging
        response["LogResult"] = base64.b64decode(response["LogResult"]).splitlines()
        response["Payload"] = json.loads(response["Payload"].read().decode("utf-8"))
        del response["ResponseMetadata"]

        events = []
        envelopes = []

        for line in response["LogResult"]:
            print("AWS:", line)
            if line.startswith(b"EVENT: "):
                line = line[len(b"EVENT: ") :]
                events.append(json.loads(line.decode("utf-8")))
            elif line.startswith(b"ENVELOPE: "):
                line = line[len(b"ENVELOPE: ") :]
                envelopes.append(json.loads(line.decode("utf-8")))
            else:
                continue

        return envelopes, events, response

    return inner


def test_basic(run_lambda_function):
    envelopes, events, response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
        init_sdk()

        def test_handler(event, context):
            raise Exception("Oh!")
        """
        ),
        b'{"foo": "bar"}',
    )

    assert response["FunctionError"] == "Unhandled"

    (event,) = events
    assert event["level"] == "error"
    (exception,) = event["exception"]["values"]
    assert exception["type"] == "Exception"
    assert exception["value"] == "Oh!"

    (frame1,) = exception["stacktrace"]["frames"]
    assert frame1["filename"] == "test_lambda.py"
    assert frame1["abs_path"] == "/var/task/test_lambda.py"
    assert frame1["function"] == "test_handler"

    assert frame1["in_app"] is True

    assert exception["mechanism"]["type"] == "aws_lambda"
    assert not exception["mechanism"]["handled"]

    assert event["extra"]["lambda"]["function_name"].startswith("test_function_")

    logs_url = event["extra"]["cloudwatch logs"]["url"]
    assert logs_url.startswith("https://console.aws.amazon.com/cloudwatch/home?region=")
    assert not re.search("(=;|=$)", logs_url)
    assert event["extra"]["cloudwatch logs"]["log_group"].startswith(
        "/aws/lambda/test_function_"
    )

    log_stream_re = "^[0-9]{4}/[0-9]{2}/[0-9]{2}/\\[[^\\]]+][a-f0-9]+$"
    log_stream = event["extra"]["cloudwatch logs"]["log_stream"]

    assert re.match(log_stream_re, log_stream)


def test_initialization_order(run_lambda_function):
    """Zappa lazily imports our code, so by the time we monkeypatch the handler
    as seen by AWS already runs. At this point at least draining the queue
    should work."""

    envelopes, events, _response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
            def test_handler(event, context):
                init_sdk()
                sentry_sdk.capture_exception(Exception("Oh!"))
        """
        ),
        b'{"foo": "bar"}',
    )

    (event,) = events

    assert event["level"] == "error"
    (exception,) = event["exception"]["values"]
    assert exception["type"] == "Exception"
    assert exception["value"] == "Oh!"


def test_request_data(run_lambda_function):
    envelopes, events, _response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
        init_sdk()
        def test_handler(event, context):
            sentry_sdk.capture_message("hi")
            return "ok"
        """
        ),
        payload=b"""
        {
          "resource": "/asd",
          "path": "/asd",
          "httpMethod": "GET",
          "headers": {
            "Host": "iwsz2c7uwi.execute-api.us-east-1.amazonaws.com",
            "User-Agent": "custom",
            "X-Forwarded-Proto": "https"
          },
          "queryStringParameters": {
            "bonkers": "true"
          },
          "pathParameters": null,
          "stageVariables": null,
          "requestContext": {
            "identity": {
              "sourceIp": "213.47.147.207",
              "userArn": "42"
            }
          },
          "body": null,
          "isBase64Encoded": false
        }
        """,
    )

    (event,) = events

    assert event["request"] == {
        "headers": {
            "Host": "iwsz2c7uwi.execute-api.us-east-1.amazonaws.com",
            "User-Agent": "custom",
            "X-Forwarded-Proto": "https",
        },
        "method": "GET",
        "query_string": {"bonkers": "true"},
        "url": "https://iwsz2c7uwi.execute-api.us-east-1.amazonaws.com/asd",
    }


def test_init_error(run_lambda_function, lambda_runtime):
    envelopes, events, response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
        init_sdk()
        func()
        """
        ),
        b'{"foo": "bar"}',
        syntax_check=False,
    )

    (event,) = events
    assert event["exception"]["values"][0]["value"] == "name 'func' is not defined"


def test_timeout_error(run_lambda_function):
    envelopes, events, response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
        init_sdk(timeout_warning=True)

        def test_handler(event, context):
            time.sleep(10)
            return 0
        """
        ),
        b'{"foo": "bar"}',
        timeout=2,
    )

    (event,) = events
    assert event["level"] == "error"
    (exception,) = event["exception"]["values"]
    assert exception["type"] == "ServerlessTimeoutWarning"
    assert exception["value"] in (
        "WARNING : Function is expected to get timed out. Configured timeout duration = 3 seconds.",
        "WARNING : Function is expected to get timed out. Configured timeout duration = 2 seconds.",
    )

    assert exception["mechanism"]["type"] == "threading"
    assert not exception["mechanism"]["handled"]

    assert event["extra"]["lambda"]["function_name"].startswith("test_function_")

    logs_url = event["extra"]["cloudwatch logs"]["url"]
    assert logs_url.startswith("https://console.aws.amazon.com/cloudwatch/home?region=")
    assert not re.search("(=;|=$)", logs_url)
    assert event["extra"]["cloudwatch logs"]["log_group"].startswith(
        "/aws/lambda/test_function_"
    )

    log_stream_re = "^[0-9]{4}/[0-9]{2}/[0-9]{2}/\\[[^\\]]+][a-f0-9]+$"
    log_stream = event["extra"]["cloudwatch logs"]["log_stream"]

    assert re.match(log_stream_re, log_stream)


def test_performance_no_error(run_lambda_function):
    envelopes, events, response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
        init_sdk(traces_sample_rate=1.0)

        def test_handler(event, context):
            return "test_string"
        """
        ),
        b'{"foo": "bar"}',
    )

    (envelope,) = envelopes

    assert envelope["type"] == "transaction"
    assert envelope["contexts"]["trace"]["op"] == "function.aws"
    assert envelope["transaction"].startswith("test_function_")
    assert envelope["transaction"] in envelope["request"]["url"]


def test_performance_error(run_lambda_function):
    envelopes, events, response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
        init_sdk(traces_sample_rate=1.0)

        def test_handler(event, context):
            raise Exception("Oh!")
        """
        ),
        b'{"foo": "bar"}',
    )

    (
        error_event,
        transaction_event,
    ) = envelopes

    assert error_event["level"] == "error"
    (exception,) = error_event["exception"]["values"]
    assert exception["type"] == "Exception"
    assert exception["value"] == "Oh!"

    assert transaction_event["type"] == "transaction"
    assert transaction_event["contexts"]["trace"]["op"] == "function.aws"
    assert transaction_event["transaction"].startswith("test_function_")
    assert transaction_event["transaction"] in transaction_event["request"]["url"]


@pytest.mark.parametrize(
    "aws_event, has_request_data, batch_size",
    [
        (b"1231", False, 1),
        (b"11.21", False, 1),
        (b'"Good dog!"', False, 1),
        (b"true", False, 1),
        (
            b"""
            [
                {"good dog": "Maisey"},
                {"good dog": "Charlie"},
                {"good dog": "Cory"},
                {"good dog": "Bodhi"}
            ]
            """,
            False,
            4,
        ),
        (
            b"""
            [
                {
                    "headers": {
                        "Host": "x.io",
                        "X-Forwarded-Proto": "http"
                    },
                    "httpMethod": "GET",
                    "path": "/somepath",
                    "queryStringParameters": {
                        "done": "true"
                    },
                    "dog": "Maisey"
                },
                {
                    "headers": {
                        "Host": "x.io",
                        "X-Forwarded-Proto": "http"
                    },
                    "httpMethod": "GET",
                    "path": "/somepath",
                    "queryStringParameters": {
                        "done": "true"
                    },
                    "dog": "Charlie"
                }
            ]
            """,
            True,
            2,
        ),
    ],
)
def test_non_dict_event(
    run_lambda_function,
    aws_event,
    has_request_data,
    batch_size,
    DictionaryContaining,  # noqa:N803
):
    envelopes, events, response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
        init_sdk(traces_sample_rate=1.0)

        def test_handler(event, context):
            raise Exception("Oh?")
        """
        ),
        aws_event,
    )

    assert response["FunctionError"] == "Unhandled"

    (
        error_event,
        transaction_event,
    ) = envelopes
    assert error_event["level"] == "error"
    assert error_event["contexts"]["trace"]["op"] == "function.aws"

    function_name = error_event["extra"]["lambda"]["function_name"]
    assert function_name.startswith("test_function_")
    assert error_event["transaction"] == function_name

    exception = error_event["exception"]["values"][0]
    assert exception["type"] == "Exception"
    assert exception["value"] == "Oh?"
    assert exception["mechanism"]["type"] == "aws_lambda"

    assert transaction_event["type"] == "transaction"
    assert transaction_event["contexts"]["trace"] == DictionaryContaining(
        error_event["contexts"]["trace"]
    )
    assert transaction_event["contexts"]["trace"]["status"] == "internal_error"
    assert transaction_event["transaction"] == error_event["transaction"]
    assert transaction_event["request"]["url"] == error_event["request"]["url"]

    if has_request_data:
        request_data = {
            "headers": {"Host": "x.io", "X-Forwarded-Proto": "http"},
            "method": "GET",
            "url": "http://x.io/somepath",
            "query_string": {
                "done": "true",
            },
        }
    else:
        request_data = {"url": "awslambda:///{}".format(function_name)}

    assert error_event["request"] == request_data
    assert transaction_event["request"] == request_data

    if batch_size > 1:
        assert error_event["tags"]["batch_size"] == batch_size
        assert error_event["tags"]["batch_request"] is True
        assert transaction_event["tags"]["batch_size"] == batch_size
        assert transaction_event["tags"]["batch_request"] is True


def test_traces_sampler_gets_correct_values_in_sampling_context(
    run_lambda_function,
    DictionaryContaining,  # noqa:N803
    ObjectDescribedBy,
    StringContaining,
):
    # TODO: This whole thing is a little hacky, specifically around the need to
    # get `conftest.py` code into the AWS runtime, which is why there's both
    # `inspect.getsource` and a copy of `_safe_is_equal` included directly in
    # the code below. Ideas which have been discussed to fix this:

    # - Include the test suite as a module installed in the package which is
    #   shot up to AWS
    # - In client.py, copy `conftest.py` (or wherever the necessary code lives)
    #   from the test suite into the main SDK directory so it gets included as
    #   "part of the SDK"

    # It's also worth noting why it's necessary to run the assertions in the AWS
    # runtime rather than asserting on side effects the way we do with events
    # and envelopes. The reasons are two-fold:

    # - We're testing against the `LambdaContext` class, which only exists in
    #   the AWS runtime
    # - If we were to transmit call args data they way we transmit event and
    #   envelope data (through JSON), we'd quickly run into the problem that all
    #   sorts of stuff isn't serializable by `json.dumps` out of the box, up to
    #   and including `datetime` objects (so anything with a timestamp is
    #   automatically out)

    # Perhaps these challenges can be solved in a cleaner and more systematic
    # way if we ever decide to refactor the entire AWS testing apparatus.

    import inspect

    envelopes, events, response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(inspect.getsource(StringContaining))
        + dedent(inspect.getsource(DictionaryContaining))
        + dedent(inspect.getsource(ObjectDescribedBy))
        + dedent(
            """
            try:
                from unittest import mock  # python 3.3 and above
            except ImportError:
                import mock  # python < 3.3

            def _safe_is_equal(x, y):
                # copied from conftest.py - see docstring and comments there
                try:
                    is_equal = x.__eq__(y)
                except AttributeError:
                    is_equal = NotImplemented

                if is_equal == NotImplemented:
                    # using == smoothes out weird variations exposed by raw __eq__
                    return x == y

                return is_equal

            def test_handler(event, context):
                # this runs after the transaction has started, which means we
                # can make assertions about traces_sampler
                try:
                    traces_sampler.assert_any_call(
                        DictionaryContaining(
                            {
                                "aws_event": DictionaryContaining({
                                    "httpMethod": "GET",
                                    "path": "/sit/stay/rollover",
                                    "headers": {"Host": "x.io", "X-Forwarded-Proto": "http"},
                                }),
                                "aws_context": ObjectDescribedBy(
                                    type=get_lambda_bootstrap().LambdaContext,
                                    attrs={
                                        'function_name': StringContaining("test_function"),
                                        'function_version': '$LATEST',
                                    }
                                )
                            }
                        )
                    )
                except AssertionError:
                    # catch the error and return it because the error itself will
                    # get swallowed by the SDK as an "internal exception"
                    return {"AssertionError raised": True,}

                return {"AssertionError raised": False,}


            traces_sampler = mock.Mock(return_value=True)

            init_sdk(
                traces_sampler=traces_sampler,
            )
        """
        ),
        b'{"httpMethod": "GET", "path": "/sit/stay/rollover", "headers": {"Host": "x.io", "X-Forwarded-Proto": "http"}}',
    )

    assert response["Payload"]["AssertionError raised"] is False


def test_serverless_no_code_instrumentation(run_lambda_function):
    """
    Test that ensures that just by adding a lambda layer containing the
    python sdk, with no code changes sentry is able to capture errors
    """

    for initial_handler in [
        None,
        "test_dir/test_lambda.test_handler",
        "test_dir.test_lambda.test_handler",
    ]:
        print("Testing Initial Handler ", initial_handler)
        envelopes, events, response = run_lambda_function(
            dedent(
                """
            import sentry_sdk

            def test_handler(event, context):
                current_client = sentry_sdk.Hub.current.client

                assert current_client is not None

                assert len(current_client.options['integrations']) == 1
                assert isinstance(current_client.options['integrations'][0],
                                  sentry_sdk.integrations.aws_lambda.AwsLambdaIntegration)

                raise Exception("Oh!")
            """
            ),
            b'{"foo": "bar"}',
            layer=True,
            initial_handler=initial_handler,
        )
        assert response["FunctionError"] == "Unhandled"
        assert response["StatusCode"] == 200

        assert response["Payload"]["errorType"] != "AssertionError"

        assert response["Payload"]["errorType"] == "Exception"
        assert response["Payload"]["errorMessage"] == "Oh!"

        assert "sentry_handler" in response["LogResult"][3].decode("utf-8")


def test_error_has_new_trace_context_performance_enabled(run_lambda_function):
    envelopes, events, response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
        init_sdk(traces_sample_rate=1.0)

        def test_handler(event, context):
            sentry_sdk.capture_message("hi")
            raise Exception("Oh!")
        """
        ),
        payload=b'{"foo": "bar"}',
    )

    (msg_event, error_event, transaction_event) = envelopes

    assert "trace" in msg_event["contexts"]
    assert "trace_id" in msg_event["contexts"]["trace"]

    assert "trace" in error_event["contexts"]
    assert "trace_id" in error_event["contexts"]["trace"]

    assert "trace" in transaction_event["contexts"]
    assert "trace_id" in transaction_event["contexts"]["trace"]

    assert (
        msg_event["contexts"]["trace"]["trace_id"]
        == error_event["contexts"]["trace"]["trace_id"]
        == transaction_event["contexts"]["trace"]["trace_id"]
    )


def test_error_has_new_trace_context_performance_disabled(run_lambda_function):
    envelopes, events, response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
        init_sdk(traces_sample_rate=None) # this is the default, just added for clarity

        def test_handler(event, context):
            sentry_sdk.capture_message("hi")
            raise Exception("Oh!")
        """
        ),
        payload=b'{"foo": "bar"}',
    )

    (msg_event, error_event) = events

    assert "trace" in msg_event["contexts"]
    assert "trace_id" in msg_event["contexts"]["trace"]

    assert "trace" in error_event["contexts"]
    assert "trace_id" in error_event["contexts"]["trace"]

    assert (
        msg_event["contexts"]["trace"]["trace_id"]
        == error_event["contexts"]["trace"]["trace_id"]
    )


def test_error_has_existing_trace_context_performance_enabled(run_lambda_function):
    trace_id = "471a43a4192642f0b136d5159a501701"
    parent_span_id = "6e8f22c393e68f19"
    parent_sampled = 1
    sentry_trace_header = "{}-{}-{}".format(trace_id, parent_span_id, parent_sampled)

    # We simulate here AWS Api Gateway's behavior of passing HTTP headers
    # as the `headers` dict in the event passed to the Lambda function.
    payload = {
        "headers": {
            "sentry-trace": sentry_trace_header,
        }
    }

    envelopes, events, response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
        init_sdk(traces_sample_rate=1.0)

        def test_handler(event, context):
            sentry_sdk.capture_message("hi")
            raise Exception("Oh!")
        """
        ),
        payload=json.dumps(payload).encode(),
    )

    (msg_event, error_event, transaction_event) = envelopes

    assert "trace" in msg_event["contexts"]
    assert "trace_id" in msg_event["contexts"]["trace"]

    assert "trace" in error_event["contexts"]
    assert "trace_id" in error_event["contexts"]["trace"]

    assert "trace" in transaction_event["contexts"]
    assert "trace_id" in transaction_event["contexts"]["trace"]

    assert (
        msg_event["contexts"]["trace"]["trace_id"]
        == error_event["contexts"]["trace"]["trace_id"]
        == transaction_event["contexts"]["trace"]["trace_id"]
        == "471a43a4192642f0b136d5159a501701"
    )


def test_error_has_existing_trace_context_performance_disabled(run_lambda_function):
    trace_id = "471a43a4192642f0b136d5159a501701"
    parent_span_id = "6e8f22c393e68f19"
    parent_sampled = 1
    sentry_trace_header = "{}-{}-{}".format(trace_id, parent_span_id, parent_sampled)

    # We simulate here AWS Api Gateway's behavior of passing HTTP headers
    # as the `headers` dict in the event passed to the Lambda function.
    payload = {
        "headers": {
            "sentry-trace": sentry_trace_header,
        }
    }

    envelopes, events, response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
        init_sdk(traces_sample_rate=None)  # this is the default, just added for clarity

        def test_handler(event, context):
            sentry_sdk.capture_message("hi")
            raise Exception("Oh!")
        """
        ),
        payload=json.dumps(payload).encode(),
    )

    (msg_event, error_event) = events

    assert "trace" in msg_event["contexts"]
    assert "trace_id" in msg_event["contexts"]["trace"]

    assert "trace" in error_event["contexts"]
    assert "trace_id" in error_event["contexts"]["trace"]

    assert (
        msg_event["contexts"]["trace"]["trace_id"]
        == error_event["contexts"]["trace"]["trace_id"]
        == "471a43a4192642f0b136d5159a501701"
    )
