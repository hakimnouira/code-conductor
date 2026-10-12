from app.core.provider_store import validate_base_url


def test_https_provider_url_is_accepted():
    assert validate_base_url("https://api.example.com/v1") == "https://api.example.com/v1"


def test_local_http_provider_url_is_accepted():
    assert validate_base_url("http://127.0.0.1:1234/v1") == "http://127.0.0.1:1234/v1"


def test_remote_plain_http_provider_url_is_rejected():
    try:
        validate_base_url("http://api.example.com/v1")
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("Remote plain HTTP must be rejected")


def test_missing_scheme_is_rejected():
    try:
        validate_base_url("api.example.com/v1")
    except ValueError:
        pass
    else:
        raise AssertionError("URLs without a scheme must be rejected")
