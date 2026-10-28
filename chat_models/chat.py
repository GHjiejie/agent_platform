import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
if api_key is None:
    raise ValueError("OPENAI_API_KEY environment variable is not set.")

api_base_url = os.getenv("OPENAI_BASE_URL")
if api_base_url is None:
    raise ValueError("OPENAI_BASE_URL environment variable is not set.")

model = os.getenv("MODEL")
if model is None:
    raise ValueError("MODEL environment variable is not set.")

chat_model = ChatOpenAI(
    model=model,
    temperature=0,
    streaming=True,
    api_key=SecretStr(api_key),
    use_responses_api=True,
    reasoning={"effort": "medium", "summary": "auto"},
    base_url=api_base_url,
)


# def get_current_model_profile() -> dict[str, Any]:
#     """Return the configured model name and its LangChain capability profile."""
#     return {
#         "model": chat_model.model_name,
#         "profile": dict(chat_model.profile) if chat_model.profile is not None else None,
#     }


# if __name__ == "__main__":
#     print(json.dumps(get_current_model_profile(), ensure_ascii=False, indent=2))
