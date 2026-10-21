from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class WorkerSettings(BaseSettings):
    model_config=SettingsConfigDict(env_file=('.env','.management.env'),extra='ignore')
    database_url: str
    prometheus_url: str='http://prometheus:9090'
    loki_url: str='http://loki:3100'
    manage_public_url: str='http://localhost:8001'
    check_interval: int=Field(default=30,ge=10,le=300)
    slack_mode: str='demo'
    slack_webhook_url: str=''
    ai_mode: str='demo'
    ai_api_key: str=''
    ai_model: str=''
    ai_endpoint: str='https://api.openai.com/v1/chat/completions'