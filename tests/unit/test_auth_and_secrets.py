import pytest
from enterprise_rag.api.auth import verify_oidc_token
from enterprise_rag.api.errors import AppError
from enterprise_rag.config import Settings
from enterprise_rag.infrastructure.secrets import EnvironmentSecretStore, SecretBindingView


def test_invalid_oidc_token_is_sanitized() -> None:
    settings = Settings(
        oidc_enabled=True,
        oidc_issuer="https://identity.example",
        oidc_jwks_url="https://identity.example/jwks",
        dev_auth_enabled=False,
    )
    with pytest.raises(AppError) as raised:
        verify_oidc_token("not-a-token", settings)
    assert raised.value.status_code == 401
    assert "not-a-token" not in raised.value.safe_message


@pytest.mark.asyncio
async def test_secret_store_and_view_never_return_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "private-value")
    store = EnvironmentSecretStore()
    assert await store.status("env://MODEL_API_KEY")
    assert await store.resolve("env://MODEL_API_KEY") == "private-value"
    view = SecretBindingView("binding-1", "env://MODEL_API_KEY", True).as_dict()
    assert view == {"id": "binding-1", "reference": "env://MODEL_API_KEY", "valid": True}
    assert "private-value" not in str(view)


@pytest.mark.asyncio
async def test_secret_store_reads_development_env_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_API_KEY=file-secret\n", encoding="utf-8")
    store = EnvironmentSecretStore(env_file)

    assert await store.status("env://MODEL_API_KEY")
    assert await store.resolve("env://MODEL_API_KEY") == "file-secret"


def test_openai_compatible_model_requires_complete_runtime_configuration() -> None:
    settings = Settings(
        model_backend="openai_compatible",
        model_api_base="",
        model_name="",
        model_api_key_reference="literal-key",
    )

    problems = settings.validate_runtime()

    assert "OpenAI-compatible model requires an API base URL" in problems
    assert "OpenAI-compatible model requires a model name" in problems
    assert "model API key must use an environment secret reference" in problems


def test_csv_environment_configuration_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_DEV_GROUPS", "administrators,operators")
    monkeypatch.setenv("RAG_ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000")
    monkeypatch.setenv("RAG_ALLOWED_EXTENSIONS", ".txt,.md")

    settings = Settings()

    assert settings.dev_groups == ["administrators", "operators"]
    assert settings.allowed_origins == ["http://localhost:5173", "http://localhost:3000"]
    assert settings.allowed_extensions == {".txt", ".md"}
