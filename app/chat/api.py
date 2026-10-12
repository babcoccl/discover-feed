"""Story Q&A routes under ``/api/v1`` (same auth as the rest of v1). ``ask`` streams the
answer as Server-Sent Events; the GET routes only read."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app import story_views as views
from app.api_v1 import PREFIX
from app.auth import require_api_key
from app.chat import store
from app.chat.prompt import CHAT_PROMPT_VERSION
from app.models import Conversation, StorySummary

router = APIRouter(prefix=PREFIX, tags=["v1"], dependencies=[Depends(require_api_key)])

MAX_QUESTION = 500
_SSE_DOC = """`text/event-stream`, in this order:
- `meta` `{conversation_id, message_id, model}`
- `waiting` `{reason, message}` (only while the model is busy with background work)
- `token` `{text}` (one per streamed chunk)
- `citations` `{citations: [{index, article_id, source, publisher, via, headline, url}]}`
- `done` `{verification: {verified, refusal, cited, checks}, tokens, latency_ms,
  time_to_first_token_ms, tokens_per_second, finish_reason, model}`
- or, instead of `citations`/`done`, `error` `{reason, message, detail, partial}` with
  `reason` one of `timeout`, `model_busy`, `unreachable`, `upstream_error`, `empty_output`,
  `http_<status>`, `story_unavailable`. A partial answer is stored as `incomplete`.

Closing the connection stops generation upstream."""


class AskV1(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    conversation_id: str | None = Field(
        default=None, description="Continue this conversation; omit to start one."
    )

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("question is empty")
        return value


class ChatCitationV1(BaseModel):
    index: int
    article_id: int
    source: str
    publisher: str
    via: str | None = None
    headline: str
    url: str


class MessageV1(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    status: Literal["complete", "streaming", "incomplete", "error"]
    error: str | None = None
    citations: list[ChatCitationV1] = []
    verification: dict | None = Field(
        default=None, description="`{verified, refusal, cited, checks}` (answers only)."
    )
    model: str = ""
    prompt_version: str = ""
    tokens: int | None = None
    latency_ms: int | None = None
    time_to_first_token_ms: int | None = None
    created_at: datetime


class ConversationV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "0b6c3f9e-8a51-4c1e-9f0e-2d7a1b5c4e10",
                    "story_id": 42,
                    "created_at": "2026-10-12T08:30:00Z",
                    "last_active_at": "2026-10-12T08:31:00Z",
                    "messages": [
                        {
                            "id": 1,
                            "role": "user",
                            "content": "When does it ship?",
                            "status": "complete",
                            "created_at": "2026-10-12T08:30:00Z",
                        },
                        {
                            "id": 2,
                            "role": "assistant",
                            "content": "The company says it ships in May [1].",
                            "status": "complete",
                            "citations": [
                                {
                                    "index": 1,
                                    "article_id": 7,
                                    "source": "Hacker News",
                                    "publisher": "ai.meta.com",
                                    "via": "Hacker News",
                                    "headline": "Meta unveils ...",
                                    "url": "https://ai.meta.com/blog/...",
                                }
                            ],
                            "verification": {"verified": True, "refusal": False, "cited": [1]},
                            "created_at": "2026-10-12T08:30:05Z",
                        },
                    ],
                }
            ]
        }
    )

    id: str
    story_id: int
    created_at: datetime
    last_active_at: datetime
    messages: list[MessageV1]


class FollowupsV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "story_id": 42,
                    "state": "ready",
                    "questions": [
                        "When will the chip ship?",
                        "Who are its first customers?",
                        "How does it compare with rivals?",
                    ],
                    "model": "qwen3.5-9b",
                    "generated_at": "2026-10-12T08:30:00Z",
                }
            ]
        }
    )

    story_id: int
    state: Literal["ready", "pending", "unavailable"] = Field(
        description="`pending`: not generated yet (they appear after the report); "
        "`unavailable`: chat is off or generation failed."
    )
    questions: list[str]
    model: str | None = None
    generated_at: datetime | None = None


class ChatStatusV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "profile": "personal-reader",
                    "state": "ok",
                    "message": "",
                    "model": "qwen3.5-9b",
                    "prompt_version": CHAT_PROMPT_VERSION,
                }
            ]
        }
    )

    profile: str | None
    state: Literal["ok", "unset", "unreachable", "unauthorized"]
    message: str
    model: str | None = None
    prompt_version: str = CHAT_PROMPT_VERSION


def _hub(request: Request):
    return getattr(request.app.state, "chat", None)


def _published(request: Request, story_id: int):
    with request.app.state.session_factory() as session:
        target = views.resolve(session, story_id)
        story = views.published_story(session, target or story_id)
    if story is None:
        raise HTTPException(status_code=404, detail=f"story {story_id} is not published")
    return story


@router.get("/chat", response_model=ChatStatusV1, summary="Is story Q&A available?")
def chat_status(
    request: Request,
    profile: Annotated[str | None, Query(description="Default: the worker's profile.")] = None,
) -> ChatStatusV1:
    hub = _hub(request)
    service = hub.for_profile(profile) if hub else None
    state, message = hub.state(profile) if hub else ("unset", "chat is off")
    return ChatStatusV1(
        profile=service.profile if service else profile,
        state=state,
        message=message,
        model=service.model if service else None,
    )


@router.post(
    "/stories/{story_id}/ask",
    summary="Ask a follow-up question (streams the answer)",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": _SSE_DOC},
        404: {"description": "Story not published, or unknown conversation."},
        503: {"description": "No chat endpoint is configured for the profile."},
    },
)
async def ask(
    request: Request,
    story_id: int,
    body: AskV1,
    profile: Annotated[
        str | None, Query(description="Whose `llm.chat` answers; default: the worker's.")
    ] = None,
) -> StreamingResponse:
    """Answers come only from the story's numbered sources, with `[n]` citations, checked
    afterwards (`done.verification`). Unpublished stories are 404."""
    hub = _hub(request)
    service = hub.for_profile(profile) if hub else None
    if service is None:
        raise HTTPException(status_code=503, detail="no chat endpoint is configured (llm.chat)")
    story = _published(request, story_id)
    started = service.start(story.story_id, body.question, body.conversation_id)
    if started is None:
        raise HTTPException(status_code=404, detail="unknown conversation for this story")
    conversation_id, message_id = started
    return StreamingResponse(
        service.ask(
            story.story_id,
            conversation_id,
            message_id,
            body.question,
            is_disconnected=request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationV1,
    summary="A conversation with its messages",
)
def get_conversation(request: Request, conversation_id: str) -> ConversationV1:
    with request.app.state.session_factory() as session:
        conv = session.get(Conversation, conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="unknown conversation")
        rows = store.messages(session, conversation_id)
    return ConversationV1(
        id=conv.id,
        story_id=conv.story_id,
        created_at=conv.created_at,
        last_active_at=conv.last_active_at,
        messages=[
            MessageV1(
                id=m.id,
                role=m.role,
                content=m.content,
                status=m.status,
                error=m.error,
                citations=m.citations_json or [],
                verification=m.verification_json,
                model=m.model,
                prompt_version=m.prompt_version,
                tokens=m.tokens,
                latency_ms=m.latency_ms,
                time_to_first_token_ms=m.time_to_first_token_ms,
                created_at=m.created_at,
            )
            for m in rows
        ],
    )


@router.delete("/conversations/{conversation_id}", status_code=204, summary="Delete a conversation")
def delete_conversation(request: Request, conversation_id: str) -> Response:
    with request.app.state.session_factory() as session:
        if not store.delete_conversation(session, conversation_id):
            raise HTTPException(status_code=404, detail="unknown conversation")
    return Response(status_code=204)


def followups_for(session, story_id: int) -> StorySummary | None:
    return session.scalar(
        select(StorySummary)
        .where(
            StorySummary.story_id == story_id,
            StorySummary.kind == "followups",
            StorySummary.status == "ok",
        )
        .order_by(StorySummary.id.desc())
        .limit(1)
    )


@router.get(
    "/stories/{story_id}/followups",
    response_model=FollowupsV1,
    summary="Up to 3 suggested follow-up questions",
)
def followups(
    request: Request,
    story_id: int,
    profile: Annotated[str | None, Query(description="Default: the worker's profile.")] = None,
) -> FollowupsV1:
    story = _published(request, story_id)
    hub = _hub(request)
    with request.app.state.session_factory() as session:
        row = followups_for(session, story.story_id)
        failed = row is None and session.scalar(
            select(StorySummary.id).where(
                StorySummary.story_id == story.story_id,
                StorySummary.kind == "followups",
                StorySummary.status == "failed",
            )
        )
    if row is not None:
        questions = list((row.content_json or {}).get("questions") or [])[:3]
        return FollowupsV1(
            story_id=story.story_id,
            state="ready",
            questions=questions,
            model=row.model,
            generated_at=row.created_at,
        )
    chat_on = hub is not None and hub.for_profile(profile) is not None
    state = "unavailable" if failed or not chat_on else "pending"
    return FollowupsV1(story_id=story.story_id, state=state, questions=[])
