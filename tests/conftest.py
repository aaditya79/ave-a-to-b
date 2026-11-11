import pytest


@pytest.fixture(autouse=True)
def fresh_limits():
    """Every test starts under the rate limits: the daily cap is low (8 a day per instance), so counts from
    earlier tests in the same run would otherwise block later ones."""
    import app
    app.asked.clear()
    app.day_count.update(day="", n=0)
    yield


@pytest.fixture(autouse=True, scope="session")
def no_model_calls():
    """No test reaches Gemini: the one path to it (LitellmModel) raises instead. Tests that need a model reply
    put a scripted model on the agent, or stub get_response themselves (both still work)."""
    from agents.extensions.models.litellm_model import LitellmModel

    async def refuse(self, *args, **kwargs):
        raise RuntimeError("a test tried to call the model; script the reply instead")

    def refuse_stream(self, *args, **kwargs):
        raise RuntimeError("a test tried to stream from the model; script the reply instead")
    real = LitellmModel.get_response, LitellmModel.stream_response
    LitellmModel.get_response, LitellmModel.stream_response = refuse, refuse_stream
    yield
    LitellmModel.get_response, LitellmModel.stream_response = real
