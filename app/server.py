# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from a2a.server.apps import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    EXTENDED_AGENT_CARD_PATH,
)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider, export
from opentelemetry.sdk.trace.export import ConsoleSpanExporter
from pydantic import BaseModel

from app.agent import app as adk_app
from app.utils.typing import Feedback

# Configure basic logging instead of Cloud Logging for local dev
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Use Console exporter for tracing locally to avoid GCP permission issues
provider = TracerProvider()
# Simple console exporter for local debugging if needed, or just a no-op
processor = export.SimpleSpanProcessor(ConsoleSpanExporter())
# provider.add_span_processor(processor) # Uncomment to see traces in console
trace.set_tracer_provider(provider)

# Use InMemory services for local development to avoid GCP dependencies
runner = Runner(
    app=adk_app,
    artifact_service=InMemoryArtifactService(),
    session_service=InMemorySessionService(),
)

request_handler = DefaultRequestHandler(
    agent_executor=A2aAgentExecutor(runner=runner), task_store=InMemoryTaskStore()
)

A2A_RPC_PATH = f"/a2a/{adk_app.name}"


async def build_dynamic_agent_card() -> AgentCard:
    """Builds the Agent Card dynamically from the root_agent."""
    agent_card_builder = AgentCardBuilder(
        agent=adk_app.root_agent,
        capabilities=AgentCapabilities(streaming=True),
        rpc_url=f"{os.getenv('APP_URL', 'http://0.0.0.0:8000')}{A2A_RPC_PATH}",
        agent_version=os.getenv("AGENT_VERSION", "0.1.0"),
    )
    agent_card = await agent_card_builder.build()
    return agent_card


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    agent_card = await build_dynamic_agent_card()
    a2a_app = A2AFastAPIApplication(agent_card=agent_card, http_handler=request_handler)
    a2a_app.add_routes_to_app(
        app_instance,
        agent_card_url=f"{A2A_RPC_PATH}{AGENT_CARD_WELL_KNOWN_PATH}",
        rpc_url=A2A_RPC_PATH,
        extended_agent_card_url=f"{A2A_RPC_PATH}{EXTENDED_AGENT_CARD_PATH}",
    )
    yield


app = FastAPI(
    title="course-creation-agent",
    description="API for interacting with the Agent course-creation-agent",
    lifespan=lifespan,
)

# Enable CORS for local development convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SimpleChatRequest(BaseModel):
    message: str
    user_id: str = "test_user"
    session_id: str = "test_session"


@app.post("/api/chat")
async def chat(request: SimpleChatRequest):
    """Simple chat endpoint for the frontend."""
    # Ensure session exists
    try:
        session = await runner.session_service.get_session(
            session_id=request.session_id, app_name=adk_app.name, user_id=request.user_id
        )
    except Exception:
        session = None
    if not session:
        session = await runner.session_service.create_session(
            app_name=adk_app.name,
            user_id=request.user_id,
            session_id=request.session_id,
        )

    user_msg = genai_types.Content(
        role="user", parts=[genai_types.Part.from_text(text=request.message)]
    )

    final_text = ""
    async for event in runner.run_async(
        user_id=request.user_id, session_id=session.id, new_message=user_msg
    ):
        # Collect text from all agent responses (including intermediate ones if desired,
        # but usually we just want the final one or we stream. For simplicity, let's
        # just concatenate all text from the agents).
        # A better approach for a real app is to check event.is_final_response()
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    # Simple concatenation, might need better formatting in a real app
                    final_text += part.text + "\n"

    return {"response": final_text.strip()}


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    # Log to console instead of Cloud Logging
    logger.info(f"Feedback received: {feedback.model_dump()}")
    return {"status": "success"}


# Mount the frontend static files.
# Ensure the 'frontend' directory exists before running.
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")

# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
