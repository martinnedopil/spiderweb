from io import BytesIO, BufferedReader
from datetime import timedelta

import pytest
from peewee import SqliteDatabase

from spiderweb import SpiderwebRouter, HttpResponse, StartupErrors
from spiderweb.constants import DEFAULT_ENCODING
from spiderweb.middleware.sessions import Session
from spiderweb.middleware import csrf
from spiderweb.tests.helpers import setup
from spiderweb.tests.views_for_tests import (
    form_view_with_csrf,
    form_csrf_exempt,
    form_view_without_csrf,
)


# app = SpiderwebRouter(
#     middleware=[
#         "spiderweb.middleware.sessions.SessionMiddleware",
#         "spiderweb.middleware.csrf.CSRFMiddleware",
#         "example_middleware.TestMiddleware",
#         "example_middleware.RedirectMiddleware",
#         "spiderweb.middleware.pydantic.PydanticMiddleware",
#         "example_middleware.ExplodingMiddleware",
#     ],
# )


def index(request):
    if "value" in request.SESSION:
        request.SESSION["value"] += 1
    else:
        request.SESSION["value"] = 0
    return HttpResponse(body=str(request.SESSION["value"]))


def test_session_middleware():
    app, environ, start_response = setup(
        middleware=["spiderweb.middleware.sessions.SessionMiddleware"],
    )

    app.add_route("/", index)

    environ["HTTP_USER_AGENT"] = "hi"
    environ["REMOTE_ADDR"] = "1.1.1.1"

    assert app(environ, start_response) == [bytes(str(0), DEFAULT_ENCODING)]

    session_key = Session.select().first().session_key
    environ["HTTP_COOKIE"] = f"swsession={session_key}"

    assert app(environ, start_response) == [bytes(str(1), DEFAULT_ENCODING)]
    assert app(environ, start_response) == [bytes(str(2), DEFAULT_ENCODING)]


def test_expired_session():
    app, environ, start_response = setup(
        middleware=["spiderweb.middleware.sessions.SessionMiddleware"],
    )

    app.add_route("/", index)

    environ["HTTP_USER_AGENT"] = "hi"
    environ["REMOTE_ADDR"] = "1.1.1.1"

    assert app(environ, start_response) == [bytes(str(0), DEFAULT_ENCODING)]

    session = Session.select().first()
    session.created_at = session.created_at - timedelta(seconds=app.session_max_age)
    session.save()

    environ["HTTP_COOKIE"] = f"swsession={session.session_key}"

    # it shouldn't increment because we get a new session
    assert app(environ, start_response) == [bytes(str(0), DEFAULT_ENCODING)]

    session2 = list(Session.select())[-1]
    assert session2.session_key != session.session_key


def test_exploding_middleware():
    app, environ, start_response = setup(
        middleware=[
            "spiderweb.tests.middleware.ExplodingRequestMiddleware",
            "spiderweb.tests.middleware.ExplodingResponseMiddleware",
        ],
    )

    app.add_route("/", index)

    assert app(environ, start_response) == [bytes(str(0), DEFAULT_ENCODING)]
    # make sure it kicked out the middleware and isn't just ignoring it
    assert len(app.middleware) == 0


def test_csrf_middleware_without_session_middleware():
    with pytest.raises(StartupErrors) as e:
        SpiderwebRouter(
            middleware=["spiderweb.middleware.csrf.CSRFMiddleware"],
            db=SqliteDatabase("spiderweb-tests.db"),
        )
    exceptiongroup = e.value.args[1]
    assert (
        exceptiongroup[0].args[0]
        == csrf.CheckForSessionMiddleware.SESSION_MIDDLEWARE_NOT_FOUND
    )


def test_csrf_middleware_above_session_middleware():
    with pytest.raises(StartupErrors) as e:
        app, environ, start_response = setup(
            middleware=[
                "spiderweb.middleware.csrf.CSRFMiddleware",
                "spiderweb.middleware.sessions.SessionMiddleware",
            ],
        )

    exceptiongroup = e.value.args[1]
    assert (
        exceptiongroup[0].args[0]
        == csrf.VerifyCorrectMiddlewarePlacement.SESSION_MIDDLEWARE_BELOW_CSRF
    )


def test_csrf_middleware():
    app, environ, start_response = setup(
        middleware=[
            "spiderweb.middleware.sessions.SessionMiddleware",
            "spiderweb.middleware.csrf.CSRFMiddleware",
        ],
    )

    app.add_route("/", form_view_with_csrf, ["GET", "POST"])

    environ["HTTP_USER_AGENT"] = "hi"
    environ["REMOTE_ADDR"] = "1.1.1.1"

    resp = app(environ, start_response)[0].decode(DEFAULT_ENCODING)

    assert "<form" in resp
    assert '<input type="hidden" name="csrf_token"' in resp

    token = resp.split('value="')[1].split('"')[0]

    formdata = f"name=bob&csrf_token={token}"
    environ["CONTENT_TYPE"] = "application/x-www-form-urlencoded"
    environ["HTTP_COOKIE"] = (
        f"swsession={[i for i in Session.select().dicts()][-1]['session_key']}"
    )
    environ["REQUEST_METHOD"] = "POST"
    environ["HTTP_X_CSRF_TOKEN"] = token
    environ["CONTENT_LENGTH"] = len(formdata)

    # setup form data
    b_handle = BytesIO()
    b_handle.write(formdata.encode(DEFAULT_ENCODING))
    b_handle.seek(0)

    environ["wsgi.input"] = BufferedReader(b_handle)

    resp2 = app(environ, start_response)[0].decode(DEFAULT_ENCODING)

    assert "bob" in resp2

    # test that it raises a CSRF error on wrong token
    formdata = f"name=bob&csrf_token=badtoken"
    b_handle = BytesIO()
    b_handle.write(formdata.encode(DEFAULT_ENCODING))
    b_handle.seek(0)

    environ["wsgi.input"] = BufferedReader(b_handle)
    resp3 = app(environ, start_response)[0].decode(DEFAULT_ENCODING)
    assert "CSRF token is invalid" in resp3

    # test that the wrong session also raises a CSRF error
    token = app.decrypt(token).split("::")[0]
    token = app.encrypt(f"{token}::badsession").decode(DEFAULT_ENCODING)
    formdata = f"name=bob&csrf_token={token}"
    b_handle = BytesIO()
    b_handle.write(formdata.encode(DEFAULT_ENCODING))
    b_handle.seek(0)

    environ["wsgi.input"] = BufferedReader(b_handle)
    resp4 = app(environ, start_response)[0].decode(DEFAULT_ENCODING)
    assert "CSRF token is invalid" in resp4


def test_csrf_expired_token():
    app, environ, start_response = setup(
        middleware=[
            "spiderweb.middleware.sessions.SessionMiddleware",
            "spiderweb.middleware.csrf.CSRFMiddleware",
        ],
    )

    app.middleware[1].CSRF_EXPIRY = -1

    app.add_route("/", form_view_with_csrf, ["GET", "POST"])

    environ["HTTP_USER_AGENT"] = "hi"
    environ["REMOTE_ADDR"] = "1.1.1.1"
    resp = app(environ, start_response)[0].decode(DEFAULT_ENCODING)
    token = resp.split('value="')[1].split('"')[0]

    formdata = f"name=bob&csrf_token={token}"
    environ["CONTENT_TYPE"] = "application/x-www-form-urlencoded"
    environ["HTTP_COOKIE"] = (
        f"swsession={[i for i in Session.select().dicts()][-1]['session_key']}"
    )
    environ["REQUEST_METHOD"] = "POST"
    environ["HTTP_ORIGIN"] = "example.com"
    environ["HTTP_X_CSRF_TOKEN"] = token
    environ["CONTENT_LENGTH"] = len(formdata)

    b_handle = BytesIO()
    b_handle.write(formdata.encode(DEFAULT_ENCODING))
    b_handle.seek(0)

    environ["wsgi.input"] = BufferedReader(b_handle)
    resp = app(environ, start_response)[0].decode(DEFAULT_ENCODING)
    assert "CSRF token is invalid" in resp


def test_csrf_exempt():
    app, environ, start_response = setup(
        middleware=[
            "spiderweb.middleware.sessions.SessionMiddleware",
            "spiderweb.middleware.csrf.CSRFMiddleware",
        ],
    )

    app.add_route("/", form_csrf_exempt, ["GET", "POST"])
    app.add_route("/2", form_view_without_csrf, ["GET", "POST"])

    environ["HTTP_USER_AGENT"] = "hi"
    environ["REMOTE_ADDR"] = "1.1.1.1"
    environ["CONTENT_TYPE"] = "application/x-www-form-urlencoded"
    environ["REQUEST_METHOD"] = "POST"

    formdata = "name=bob"
    environ["CONTENT_LENGTH"] = len(formdata)
    b_handle = BytesIO()
    b_handle.write(formdata.encode(DEFAULT_ENCODING))
    b_handle.seek(0)

    environ["wsgi.input"] = BufferedReader(b_handle)
    resp = app(environ, start_response)[0].decode(DEFAULT_ENCODING)
    assert "bob" in resp

    environ["PATH_INFO"] = "/2"
    resp2 = app(environ, start_response)[0].decode(DEFAULT_ENCODING)
    assert "CSRF token is invalid" in resp2


def test_csrf_trusted_origins():
    app, environ, start_response = setup(
        middleware=[
            "spiderweb.middleware.sessions.SessionMiddleware",
            "spiderweb.middleware.csrf.CSRFMiddleware",
        ],
        csrf_trusted_origins=[
            "example.com",
        ],
    )
    app.add_route("/", form_view_without_csrf, ["GET", "POST"])

    environ["HTTP_USER_AGENT"] = "hi"
    environ["REMOTE_ADDR"] = "1.1.1.1"
    environ["CONTENT_TYPE"] = "application/x-www-form-urlencoded"
    environ["REQUEST_METHOD"] = "POST"

    formdata = "name=bob"
    environ["CONTENT_LENGTH"] = len(formdata)
    b_handle = BytesIO()
    b_handle.write(formdata.encode(DEFAULT_ENCODING))
    b_handle.seek(0)
    environ["wsgi.input"] = BufferedReader(b_handle)

    environ["HTTP_ORIGIN"] = "notvalid.com"
    resp = app(environ, start_response)[0].decode(DEFAULT_ENCODING)
    assert "CSRF token is invalid" in resp

    b_handle = BytesIO()
    b_handle.write(formdata.encode(DEFAULT_ENCODING))
    b_handle.seek(0)
    environ["wsgi.input"] = BufferedReader(b_handle)

    environ["HTTP_ORIGIN"] = "example.com"
    resp2 = app(environ, start_response)[0].decode(DEFAULT_ENCODING)
    assert resp2 == '{"name": "bob"}'
