from enterprise_rag.infrastructure.redaction import redact


def test_redacts_sensitive_keys_and_bearer_tokens() -> None:
    value = {
        "api_key": "super-secret",
        "message": "Authorization: Bearer ey.secret.token",
        "nested": [{"password": "bad"}],
    }
    result = redact(value)
    assert result["api_key"] == "[REDACTED]"
    assert "ey.secret.token" not in result["message"]
    assert result["nested"][0]["password"] == "[REDACTED]"


def test_evaluation_export_redacts_secret_fields_and_restricted_content() -> None:
    result = redact(
        {
            "items": [
                {
                    "details": {
                        "api_key": "must-not-leak",
                        "answer": "identifier 123-45-6789",
                    }
                }
            ]
        }
    )
    assert result["items"][0]["details"]["api_key"] == "[REDACTED]"
    assert result["items"][0]["details"]["answer"] == "identifier [REDACTED ID]"
