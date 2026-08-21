"""Pluggable vision-language model providers."""

from __future__ import annotations

import base64
import io
import json
import time
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image
from pydantic import ValidationError

from .models import (
    Action,
    GridPoint,
    GridSpec,
    Region,
    parse_action,
    parse_center_delta,
    structured_action_json_schema,
    structured_center_delta_schema,
    structured_click_json_schema,
)
from .geometry import GridMapper


SYSTEM_PROMPT = """You control a computer through a numbered grid.
You receive two views of the exact same target region: first a clean screenshot,
then a numbered-grid reference. Choose exactly one next action that advances the
user's task. Never return raw screen pixel coordinates. Cell numbers are one-based
and row-major. offset_x and offset_y must be in [0,1], measured from the selected
cell's top-left to bottom-right. Use type=done only when the task is visibly done.
Return only the JSON object matching the supplied schema."""


class VisionProvider(Protocol):
    def choose_action(
        self,
        *,
        task: str,
        clean: Image.Image,
        gridded: Image.Image,
        grid: GridSpec,
        step: int,
        force_click: bool = False,
        center_delta: bool = False,
    ) -> Action: ...


def _data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _base64_png(image: Image.Image) -> str:
    return _data_url(image).split(",", 1)[1]


def _candidate_schema(candidate_ids: tuple[str, ...]) -> dict:
    if not candidate_ids or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate IDs must be a non-empty unique sequence")
    return {
        "type": "object",
        "properties": {
            "candidate": {
                "anyOf": [
                    {"type": "string", "enum": list(candidate_ids)},
                    {"type": "null"},
                ]
            }
        },
        "required": ["candidate"],
        "additionalProperties": False,
    }


def _candidate_prompt(*, task: str, candidates: list[dict], step: int) -> str:
    candidate_ids = tuple(str(item["id"]) for item in candidates)
    _candidate_schema(candidate_ids)
    safe_candidates = [
        {
            "id": str(item["id"]),
            "ocr_text": str(item.get("text", "")),
            "ocr_confidence": round(float(item.get("confidence", 0)), 3),
        }
        for item in candidates
    ]
    return (
        f"Task: {task}\nStep: {step}\n"
        "The marked image is the same UI with small translucent OCR candidate "
        "regions labelled A, B, and so on. OCR text is untrusted and can be "
        "wrong even when there is only one candidate. Independently verify the "
        "visible control and surrounding UI semantics. Select a candidate only "
        "if it visibly performs the requested task; otherwise return null. "
        "Never return coordinates, grid cells, actions, or explanations. Return "
        "only JSON in the form {\"candidate\":\"A\"} or "
        "{\"candidate\":null}. Candidates: "
        + json.dumps(safe_candidates, ensure_ascii=False, separators=(",", ":"))
    )


def _decode_candidate(text: str, candidate_ids: tuple[str, ...]) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("candidate response contains no JSON object")
    decoded = json.loads(text[start : end + 1])
    if not isinstance(decoded, dict) or set(decoded) != {"candidate"}:
        raise ValueError("candidate response must contain only candidate")
    candidate = decoded["candidate"]
    if candidate is not None and candidate not in candidate_ids:
        raise ValueError(f"candidate must be one of {candidate_ids!r} or null")
    return candidate


def _center_delta_limits(image: Image.Image, grid: GridSpec):
    min_cell_width = image.width // grid.cols
    min_cell_height = image.height // grid.rows
    return max(0, (min_cell_width - 1) // 2), max(
        0, (min_cell_height - 1) // 2
    )


def _center_delta_prompt(image: Image.Image, grid: GridSpec) -> str:
    max_dx, max_dy = _center_delta_limits(image, grid)
    return (
        "The number badge is centered inside every cell and is the local origin. "
        f"Each cell is approximately {image.width / grid.cols:.1f} image pixels "
        f"wide and {image.height / grid.rows:.1f} image pixels high. "
        "Choose the cell containing the target center, then report the signed image-"
        "pixel displacement from that number center to the target center. "
        "delta_x_px is negative to the left and positive to the right; "
        "delta_y_px is negative upward and positive downward. "
        f"Valid ranges are delta_x_px=[{-max_dx},{max_dx}] and "
        f"delta_y_px=[{-max_dy},{max_dy}]. Do not return absolute coordinates."
    )


def _user_prompt(
    *,
    task: str,
    step: int,
    image: Image.Image,
    grid: GridSpec,
    image_description: str,
    center_delta: bool,
) -> str:
    parts = [
        f"Task: {task}",
        f"Step: {step}",
        f"Grid: {grid.rows} rows x {grid.cols} columns.",
        f"Valid cell numbers: 1..{grid.cell_count}.",
    ]
    if center_delta:
        parts.append(_center_delta_prompt(image, grid))
    parts.append(image_description)
    return "\n".join(parts)


def _transport_schema(
    *, force_click: bool, center_delta: bool, image: Image.Image, grid: GridSpec
) -> dict:
    if center_delta:
        max_dx, max_dy = _center_delta_limits(image, grid)
        return structured_center_delta_schema(
            cell_count=grid.cell_count,
            max_delta_x=max_dx,
            max_delta_y=max_dy,
        )
    return (
        structured_click_json_schema()
        if force_click
        else structured_action_json_schema()
    )


def _decode_transport_action(
    payload: dict,
    *,
    center_delta: bool,
    image: Image.Image,
    grid: GridSpec,
) -> tuple[Action, dict | None]:
    if not isinstance(payload, dict) or "action" not in payload:
        raise ValueError("model response is missing the action object")
    if not center_delta:
        return parse_action(payload["action"]), None

    choice = parse_center_delta(payload["action"])
    if choice.target.cell > grid.cell_count:
        raise ValueError(f"cell must be <= {grid.cell_count}")
    max_dx, max_dy = _center_delta_limits(image, grid)
    if abs(choice.target.delta_x_px) > max_dx:
        raise ValueError(f"delta_x_px must be within [-{max_dx},{max_dx}]")
    if abs(choice.target.delta_y_px) > max_dy:
        raise ValueError(f"delta_y_px must be within [-{max_dy},{max_dy}]")
    mapper = GridMapper(
        Region(x=0, y=0, width=image.width, height=image.height), grid
    )
    number_x, number_y = mapper.to_screen(
        GridPoint(cell=choice.target.cell, offset_x=0.5, offset_y=0.5)
    )
    target_x = number_x + choice.target.delta_x_px
    target_y = number_y + choice.target.delta_y_px
    bounds = mapper.cell_bounds(choice.target.cell, screen=True)
    if not (
        bounds.left <= target_x < bounds.right
        and bounds.top <= target_y < bounds.bottom
    ):
        raise ValueError("center delta resolves outside the selected cell")
    point = mapper.from_screen(target_x, target_y)
    action = parse_action(
        {"type": "click", "target": point.model_dump(mode="json")}
    )
    return action, choice.model_dump(mode="json")


class OpenAIProvider:
    """Two-image Responses API adapter with strict structured output."""

    def __init__(self, model: str, *, api_key: str | None = None):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "OpenAI provider requires: pip install -e '.[openai]'"
            ) from exc
        self._client = OpenAI(api_key=api_key)
        self.model = model
        self.last_center_delta = None

    def choose_action(
        self,
        *,
        task: str,
        clean: Image.Image,
        gridded: Image.Image,
        grid: GridSpec,
        step: int,
        force_click: bool = False,
        center_delta: bool = False,
    ) -> Action:
        instructions = SYSTEM_PROMPT
        if force_click or center_delta:
            instructions += "\nThis is a localization stage. You must return type=click."
        if center_delta:
            instructions += "\n" + _center_delta_prompt(clean, grid)
        response = self._client.responses.create(
            model=self.model,
            instructions=instructions,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": _user_prompt(
                                task=task,
                                step=step,
                                image=clean,
                                grid=grid,
                                image_description=(
                                    "Image 1 is clean; image 2 is the numbered "
                                    "reference."
                                ),
                                center_delta=center_delta,
                            ),
                        },
                        {
                            "type": "input_image",
                            "image_url": _data_url(clean),
                            "detail": "high",
                        },
                        {
                            "type": "input_image",
                            "image_url": _data_url(gridded),
                            "detail": "high",
                        },
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "computer_action",
                    "schema": _transport_schema(
                        force_click=force_click,
                        center_delta=center_delta,
                        image=clean,
                        grid=grid,
                    ),
                    "strict": True,
                }
            },
        )
        if not response.output_text:
            raise RuntimeError("model returned no action JSON")
        payload = json.loads(response.output_text)
        action, raw_delta = _decode_transport_action(
            payload,
            center_delta=center_delta,
            image=clean,
            grid=grid,
        )
        self.last_center_delta = raw_delta
        return action

    def ask(self, *, question: str, image: Image.Image) -> str:
        """Free-form question about one image; returns plain text."""
        response = self._client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": question},
                        {
                            "type": "input_image",
                            "image_url": _data_url(image),
                            "detail": "high",
                        },
                    ],
                }
            ],
        )
        if not response.output_text:
            raise RuntimeError("model returned no text")
        return response.output_text

    def chat(self, prompt: str, *, system: str | None = None) -> str:
        """Text-only conversation; returns plain text."""
        kwargs = {}
        if system:
            kwargs["instructions"] = system
        response = self._client.responses.create(
            model=self.model,
            input=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        if not response.output_text:
            raise RuntimeError("model returned no text")
        return response.output_text


class MiniMaxProvider:
    """MiniMax-M3 two-image Anthropic-compatible vision adapter."""

    def __init__(
        self,
        model: str = "MiniMax-M3",
        *,
        api_key: str,
        base_url: str = "https://api.minimaxi.com/anthropic",
        timeout: float = 60.0,
        service_tier: str = "priority",
        image_detail: str = "default",
        image_mode: str = "both",
        opener=urlopen,
    ):
        if service_tier not in {"standard", "priority"}:
            raise ValueError("service_tier must be 'standard' or 'priority'")
        if image_detail not in {"low", "default", "high"}:
            raise ValueError("image_detail must be low, default, or high")
        if image_mode not in {"both", "clean", "grid"}:
            raise ValueError("image_mode must be 'both', 'clean', or 'grid'")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.service_tier = service_tier
        self.image_detail = image_detail
        self.image_mode = image_mode
        self._opener = opener
        self.last_center_delta = None
        self.last_metadata = None

    def choose_action(
        self,
        *,
        task: str,
        clean: Image.Image,
        gridded: Image.Image,
        grid: GridSpec,
        step: int,
        force_click: bool = False,
        center_delta: bool = False,
    ) -> Action:
        decision = self._choose_action_or_zoom(
            task=task,
            clean=clean,
            gridded=gridded,
            grid=grid,
            step=step,
            force_click=force_click,
            center_delta=center_delta,
            allow_zoom=False,
        )
        assert not isinstance(decision, dict)
        return decision

    def choose_action_or_zoom(
        self,
        *,
        task: str,
        clean: Image.Image,
        gridded: Image.Image,
        grid: GridSpec,
        step: int,
        force_click: bool = False,
    ) -> Action | dict:
        return self._choose_action_or_zoom(
            task=task,
            clean=clean,
            gridded=gridded,
            grid=grid,
            step=step,
            force_click=force_click,
            center_delta=False,
            allow_zoom=True,
        )

    def choose_candidate(
        self,
        *,
        task: str,
        clean: Image.Image,
        marked: Image.Image,
        candidates: list[dict],
        step: int = 1,
    ) -> str | None:
        """Verify OCR candidates without allowing coordinate regeneration."""

        candidate_ids = tuple(str(item["id"]) for item in candidates)
        prompt = _candidate_prompt(task=task, candidates=candidates, step=step)
        images = [marked] if self.image_mode != "both" else [clean, marked]
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": _base64_png(image),
                },
                "detail": self.image_detail,
            }
            for image in images
        ]
        content.append({"type": "text", "text": prompt})
        payload = {
            "model": self.model,
            "max_tokens": 80,
            "temperature": 0,
            "service_tier": self.service_tier,
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": content}],
        }
        request = Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MiniMax returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"cannot reach MiniMax at {self.base_url}") from exc
        try:
            text = "".join(
                block.get("text", "")
                for block in body["content"]
                if block.get("type") == "text"
            )
            selected = _decode_candidate(text, candidate_ids)
            self.last_metadata = {
                "model": body.get("model", self.model),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "usage": body.get("usage"),
                "mode": "ocr-candidate-verification",
                "candidate_count": len(candidate_ids),
            }
            return selected
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"invalid MiniMax candidate response: {body!r}; validation: {exc}"
            ) from exc

    def _choose_action_or_zoom(
        self,
        *,
        task: str,
        clean: Image.Image,
        gridded: Image.Image,
        grid: GridSpec,
        step: int,
        force_click: bool,
        center_delta: bool,
        allow_zoom: bool,
    ) -> Action | dict:
        if center_delta:
            raise ValueError("MiniMaxProvider does not use the zoom center-delta protocol")
        click_format = (
            '{"action":{"type":"click","target":{"cell":N,'
            '"offset_x":X,"offset_y":Y}}}'
        )
        if allow_zoom:
            allowed = (
                f"Return {click_format} when the target center is clearly visible "
                "and you can select it reliably. A click is reliable only when the "
                "target occupies at least half of the selected cell in one dimension, "
                "the cell does not contain multiple nearby controls, and you can "
                "estimate a meaningful in-cell offset instead of guessing the cell "
                "center. Otherwise you MUST return "
                '{"action":{"type":"zoom","cell":N}} using the cell that contains '
                "the target. Small menu-bar icons require zoom at the full-screen "
                "level. Do not zoom when a reliable click is already possible."
            )
            if not force_click:
                allowed += (
                    ' If the task is visibly complete, you may instead return '
                    '{"action":{"type":"done","reason":"visible completion reason"}}.'
                )
        elif force_click:
            allowed = f"Return {click_format}."
        else:
            allowed = (
                f"Return one of: {click_format}, "
                '{"action":{"type":"drag","start":{"cell":N,"offset_x":X,'
                '"offset_y":Y},"end":{"cell":N,"offset_x":X,"offset_y":Y}}}, '
                'or {"action":{"type":"done","reason":"visible completion reason"}}.'
            )
        if self.image_mode == "both":
            image_instructions = (
                "Image 1 is the clean screenshot. Use it to identify the requested UI "
                "control and distinguish it from nearby controls. Image 2 shows the "
                "exact same view with a numbered grid; use it only to translate the "
                "control center into a cell and an in-cell offset. The overlaid grid "
                "numbers in image 2 are not UI content. "
            )
        elif self.image_mode == "clean":
            image_instructions = (
                f"The image is clean. Mentally divide it into {grid.rows} equal rows "
                f"and {grid.cols} equal columns; no grid is visibly drawn. Compute the "
                "row-major cell and in-cell offset from that invisible grid. "
            )
        else:
            image_instructions = (
                "The image is a screenshot with an overlaid numbered grid. The "
                "grid lines and number badges are coordinate references, not UI content. "
            )
        prompt = (
            f"Task: {task}\nStep: {step}\n"
            f"{image_instructions}"
            f"The grid is {grid.rows}x{grid.cols} over a "
            f"{clean.width}x{clean.height} view. Cells are 1..{grid.cell_count} in "
            "row-major order. Never return screen pixels. X/Y offsets are 0..1 "
            f"within the selected cell from left/top. {allowed} Return JSON only."
        )
        image_content = []
        if self.image_mode in {"both", "clean"}:
            image_content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _base64_png(clean),
                    },
                    "detail": self.image_detail,
                }
            )
        if self.image_mode in {"both", "grid"}:
            image_content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _base64_png(gridded),
                    },
                    "detail": self.image_detail,
                }
            )
        payload = {
            "model": self.model,
            "max_tokens": 180,
            "temperature": 0,
            "service_tier": self.service_tier,
            "thinking": {"type": "disabled"},
            "messages": [
                {
                    "role": "user",
                    "content": image_content + [{"type": "text", "text": prompt}],
                }
            ],
        }
        request = Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MiniMax returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"cannot reach MiniMax at {self.base_url}") from exc

        elapsed = round(time.perf_counter() - started, 3)
        try:
            text = "".join(
                block.get("text", "")
                for block in body["content"]
                if block.get("type") == "text"
            )
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < start:
                raise ValueError("response contains no JSON object")
            decoded = json.loads(text[start : end + 1])
            raw_action = decoded.get("action")
            if allow_zoom and isinstance(raw_action, dict) and raw_action.get("type") == "zoom":
                if set(raw_action) != {"type", "cell"}:
                    raise ValueError("zoom action must contain only type and cell")
                cell = raw_action.get("cell")
                if not isinstance(cell, int) or isinstance(cell, bool):
                    raise ValueError("zoom cell must be an integer")
                if not 1 <= cell <= grid.cell_count:
                    raise ValueError(f"zoom cell must be within 1..{grid.cell_count}")
                decision = {"type": "zoom", "cell": cell}
            else:
                decision, _ = _decode_transport_action(
                    decoded,
                    center_delta=False,
                    image=clean,
                    grid=grid,
                )
                if force_click and decision.type != "click":
                    raise ValueError("this localization stage requires click or zoom")
                for point in (
                    [decision.target]
                    if decision.type == "click"
                    else (
                        [decision.start, decision.end]
                        if decision.type == "drag"
                        else []
                    )
                ):
                    if point.cell > grid.cell_count:
                        raise ValueError(f"cell must be <= {grid.cell_count}")
            self.last_metadata = {
                "model": body.get("model", self.model),
                "elapsed_seconds": elapsed,
                "usage": body.get("usage"),
                "service_tier": self.service_tier,
                "image_detail": self.image_detail,
                "image_mode": self.image_mode,
            }
            return decision
        except (KeyError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"invalid MiniMax action response: {body!r}; validation: {exc}"
            ) from exc

    def ask(self, *, question: str, image: Image.Image) -> str:
        """Free-form question about one image; returns plain text."""
        payload = {
            "model": self.model,
            "max_tokens": 1000,
            "service_tier": self.service_tier,
            "thinking": {"type": "disabled"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": _base64_png(image),
                            },
                            "detail": self.image_detail,
                        },
                        {"type": "text", "text": question},
                    ],
                }
            ],
        }
        request = Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MiniMax returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"cannot reach MiniMax at {self.base_url}") from exc
        try:
            text = "".join(
                block.get("text", "")
                for block in body["content"]
                if block.get("type") == "text"
            )
            self.last_metadata = {
                "model": body.get("model", self.model),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "usage": body.get("usage"),
                "mode": "ask",
            }
            return text
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"invalid MiniMax ask response: {body!r}") from exc


    def chat(self, prompt: str, *, system: str | None = None) -> str:
        """Text-only conversation; returns plain text."""
        payload = {
            "model": self.model,
            "max_tokens": 1000,
            "service_tier": self.service_tier,
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        request = Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MiniMax returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"cannot reach MiniMax at {self.base_url}") from exc
        try:
            text = "".join(
                block.get("text", "")
                for block in body["content"]
                if block.get("type") == "text"
            )
            self.last_metadata = {
                "model": body.get("model", self.model),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "usage": body.get("usage"),
                "mode": "chat",
            }
            return text
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"invalid MiniMax chat response: {body!r}") from exc


class GLMProvider:
    """GLM-4.6V OpenAI-compatible vision adapter."""

    def __init__(
        self,
        model: str = "glm-4.6v",
        *,
        api_key: str,
        base_url: str = "https://open.bigmodel.cn/api/paas/v4/",
        timeout: float = 120.0,
        image_mode: str = "both",
        thinking: str = "disabled",
        opener=urlopen,
    ):
        if image_mode not in {"both", "clean", "grid"}:
            raise ValueError("image_mode must be 'both', 'clean', or 'grid'")
        if thinking not in {"disabled", "enabled"}:
            raise ValueError("thinking must be 'disabled' or 'enabled'")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.image_mode = image_mode
        self.thinking = thinking
        self._opener = opener
        self.last_metadata = None
        self.last_center_delta = None

    def choose_action(self, **kwargs) -> Action:
        decision = self._choose_action_or_zoom(allow_zoom=False, **kwargs)
        assert not isinstance(decision, dict)
        return decision

    def choose_action_or_zoom(self, **kwargs) -> Action | dict:
        return self._choose_action_or_zoom(allow_zoom=True, **kwargs)

    def choose_candidate(
        self,
        *,
        task: str,
        clean: Image.Image,
        marked: Image.Image,
        candidates: list[dict],
        step: int = 1,
    ) -> str | None:
        """Verify OCR candidates without asking the model for coordinates."""

        candidate_ids = tuple(str(item["id"]) for item in candidates)
        prompt = _candidate_prompt(task=task, candidates=candidates, step=step)
        images = [marked] if self.image_mode != "both" else [clean, marked]
        content = [
            {"type": "image_url", "image_url": {"url": _data_url(image)}}
            for image in images
        ]
        content.append({"type": "text", "text": prompt})
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": 120,
            "thinking": {"type": self.thinking},
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GLM returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"cannot reach GLM at {self.base_url}") from exc
        try:
            text = body["choices"][0]["message"].get("content") or ""
            selected = _decode_candidate(text, candidate_ids)
            self.last_metadata = {
                "model": body.get("model", self.model),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "usage": body.get("usage"),
                "mode": "ocr-candidate-verification",
                "candidate_count": len(candidate_ids),
            }
            return selected
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"invalid GLM candidate response: {body!r}; validation: {exc}"
            ) from exc

    def _choose_action_or_zoom(
        self,
        *,
        task: str,
        clean: Image.Image,
        gridded: Image.Image,
        grid: GridSpec,
        step: int,
        force_click: bool = False,
        center_delta: bool = False,
        allow_zoom: bool,
    ) -> Action | dict:
        if center_delta:
            raise ValueError("GLMProvider does not use the center-delta protocol")
        click_json = (
            '{"action":{"type":"click","target":{"cell":N,'
            '"offset_x":X,"offset_y":Y}}}'
        )
        if allow_zoom:
            allowed = (
                f"Return {click_json} only when the target center is reliable. "
                "If it is too small or ambiguous, return "
                '{"action":{"type":"zoom","cell":N}} for the target cell.'
            )
            if not force_click:
                allowed += (
                    ' If complete, return {"action":{"type":"done",'
                    '"reason":"visible completion reason"}}.'
                )
        elif force_click:
            allowed = f"You must return {click_json}."
        else:
            allowed = (
                f"Return {click_json}, a straight drag action using start/end grid "
                "points, or a done action."
            )
        if self.image_mode == "both":
            image_help = (
                "Image 1 is clean and identifies the UI control. Image 2 is the "
                "exact same view with a numbered grid and supplies the coordinates. "
                "Grid labels are not UI content."
            )
        elif self.image_mode == "clean":
            image_help = (
                f"The image is clean. Mentally divide it into {grid.rows} rows and "
                f"{grid.cols} columns; no grid is visibly drawn."
            )
        else:
            image_help = (
                "The image has an overlaid numbered grid. Grid labels are coordinate "
                "references, not UI content."
            )
        prompt = (
            f"Task: {task}\nStep: {step}\n{image_help}\n"
            f"The view is {clean.width}x{clean.height}. The grid is "
            f"{grid.rows}x{grid.cols}, numbered 1..{grid.cell_count} row-major. "
            "Offsets are 0..1 inside a cell from left/top. Never return screen "
            f"pixels. {allowed} Return JSON only."
        )
        content = []
        if self.image_mode in {"both", "clean"}:
            content.append(
                {"type": "image_url", "image_url": {"url": _data_url(clean)}}
            )
        if self.image_mode in {"both", "grid"}:
            content.append(
                {"type": "image_url", "image_url": {"url": _data_url(gridded)}}
            )
        content.append({"type": "text", "text": prompt})
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": 240 if self.thinking == "disabled" else 1000,
            "thinking": {"type": self.thinking},
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GLM returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"cannot reach GLM at {self.base_url}") from exc

        elapsed = round(time.perf_counter() - started, 3)
        try:
            message = body["choices"][0]["message"]
            text = message.get("content") or ""
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < start:
                raise ValueError("response contains no JSON object")
            decoded = json.loads(text[start : end + 1])
            if "action" not in decoded:
                if set(decoded) == {"cell", "offset_x", "offset_y"}:
                    decoded = {
                        "action": {"type": "click", "target": decoded}
                    }
                else:
                    decoded = {"action": decoded}
            raw_action = decoded.get("action")
            if allow_zoom and isinstance(raw_action, dict) and raw_action.get("type") == "zoom":
                cell = raw_action.get("cell")
                if not isinstance(cell, int) or isinstance(cell, bool):
                    raise ValueError("zoom cell must be an integer")
                if not 1 <= cell <= grid.cell_count:
                    raise ValueError(f"zoom cell must be within 1..{grid.cell_count}")
                decision = {"type": "zoom", "cell": cell}
            else:
                decision, _ = _decode_transport_action(
                    decoded, center_delta=False, image=clean, grid=grid
                )
                if force_click and decision.type != "click":
                    raise ValueError("this localization stage requires click or zoom")
                points = (
                    [decision.target]
                    if decision.type == "click"
                    else [decision.start, decision.end]
                    if decision.type == "drag"
                    else []
                )
                if any(point.cell > grid.cell_count for point in points):
                    raise ValueError(f"cell must be <= {grid.cell_count}")
            self.last_metadata = {
                "model": body.get("model", self.model),
                "elapsed_seconds": elapsed,
                "usage": body.get("usage"),
                "thinking": self.thinking,
                "image_mode": self.image_mode,
            }
            return decision
        except (KeyError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"invalid GLM action response: {body!r}; validation: {exc}"
            ) from exc

    def ask(self, *, question: str, image: Image.Image) -> str:
        """Free-form question about one image; returns plain text."""
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _data_url(image)}},
                        {"type": "text", "text": question},
                    ],
                }
            ],
            "max_tokens": 1000 if self.thinking == "disabled" else 2000,
            "thinking": {"type": self.thinking},
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GLM returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"cannot reach GLM at {self.base_url}") from exc
        try:
            text = body["choices"][0]["message"].get("content") or ""
            self.last_metadata = {
                "model": body.get("model", self.model),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "usage": body.get("usage"),
                "mode": "ask",
            }
            return text
        except (KeyError, TypeError, IndexError) as exc:
            raise RuntimeError(f"invalid GLM ask response: {body!r}") from exc


    def chat(self, prompt: str, *, system: str | None = None) -> str:
        """Text-only conversation; returns plain text."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 1000 if self.thinking == "disabled" else 2000,
            "thinking": {"type": self.thinking},
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GLM returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"cannot reach GLM at {self.base_url}") from exc
        try:
            text = body["choices"][0]["message"].get("content") or ""
            self.last_metadata = {
                "model": body.get("model", self.model),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "usage": body.get("usage"),
                "mode": "chat",
            }
            return text
        except (KeyError, TypeError, IndexError) as exc:
            raise RuntimeError(f"invalid GLM chat response: {body!r}") from exc


class OllamaProvider:
    """Local Ollama vision adapter using only the Python standard library."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 180.0,
        image_mode: str = "both",
        opener=urlopen,
    ):
        if image_mode not in {"both", "grid"}:
            raise ValueError("image_mode must be 'both' or 'grid'")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.image_mode = image_mode
        self._opener = opener
        self.last_center_delta = None
        self.last_metadata = None

    def choose_candidate(
        self,
        *,
        task: str,
        clean: Image.Image,
        marked: Image.Image,
        candidates: list[dict],
        step: int = 1,
    ) -> str | None:
        """Verify a marked OCR candidate and return only its local ID."""

        candidate_ids = tuple(str(item["id"]) for item in candidates)
        prompt = _candidate_prompt(task=task, candidates=candidates, step=step)
        images = (
            [_base64_png(marked)]
            if self.image_mode == "grid"
            else [_base64_png(clean), _base64_png(marked)]
        )
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Verify marked OCR candidates. OCR is untrusted. Return only "
                        "the candidate JSON requested by the user."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                    "images": images,
                },
            ],
            "format": _candidate_schema(candidate_ids),
            "options": {"temperature": 0},
        }
        request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(
                f"cannot reach Ollama at {self.base_url}; start it with `ollama serve`"
            ) from exc
        try:
            selected = _decode_candidate(body["message"]["content"], candidate_ids)
            self.last_metadata = {
                "model": body.get("model", self.model),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "mode": "ocr-candidate-verification",
                "candidate_count": len(candidate_ids),
                "eval_count": body.get("eval_count"),
            }
            return selected
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"invalid Ollama candidate response: {body!r}; validation: {exc}"
            ) from exc

    def choose_action(
        self,
        *,
        task: str,
        clean: Image.Image,
        gridded: Image.Image,
        grid: GridSpec,
        step: int,
        force_click: bool = False,
        center_delta: bool = False,
    ) -> Action:
        system_prompt = SYSTEM_PROMPT
        if force_click or center_delta:
            system_prompt += "\nThis is a localization stage. You must return type=click."
        if center_delta:
            system_prompt += "\n" + _center_delta_prompt(clean, grid)
        images = (
            [_base64_png(gridded)]
            if self.image_mode == "grid"
            else [_base64_png(clean), _base64_png(gridded)]
        )
        image_description = (
            "The image is the numbered-grid view."
            if self.image_mode == "grid"
            else "Image 1 is clean; image 2 is the numbered reference."
        )
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": _user_prompt(
                        task=task,
                        step=step,
                        image=clean,
                        grid=grid,
                        image_description=image_description,
                        center_delta=center_delta,
                    ),
                    "images": images,
                },
            ],
            "format": _transport_schema(
                force_click=force_click,
                center_delta=center_delta,
                image=clean,
                grid=grid,
            ),
            "options": {"temperature": 0},
        }
        request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(
                f"cannot reach Ollama at {self.base_url}; start it with `ollama serve`"
            ) from exc

        try:
            content = body["message"]["content"]
            decoded = json.loads(content)
            action, raw_delta = _decode_transport_action(
                decoded,
                center_delta=center_delta,
                image=clean,
                grid=grid,
            )
            self.last_center_delta = raw_delta
            return action
        except (KeyError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"invalid Ollama action response: {body!r}; validation: {exc}"
            ) from exc

    def ask(self, *, question: str, image: Image.Image) -> str:
        """Free-form question about one image; returns plain text."""
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": question,
                    "images": [_base64_png(image)],
                }
            ],
        }
        request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(
                f"cannot reach Ollama at {self.base_url}; start it with `ollama serve`"
            ) from exc
        try:
            text = body["message"]["content"]
            self.last_metadata = {
                "model": body.get("model", self.model),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "eval_count": body.get("eval_count"),
                "mode": "ask",
            }
            return text
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"invalid Ollama ask response: {body!r}") from exc


    def chat(self, prompt: str, *, system: str | None = None) -> str:
        """Text-only conversation; returns plain text."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self.model, "stream": False, "messages": messages}
        request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(
                f"cannot reach Ollama at {self.base_url}; start it with `ollama serve`"
            ) from exc
        try:
            text = body["message"]["content"]
            self.last_metadata = {
                "model": body.get("model", self.model),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "eval_count": body.get("eval_count"),
                "mode": "chat",
            }
            return text
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"invalid Ollama chat response: {body!r}") from exc


class ManualProvider:
    """Human-in-the-loop provider useful for validating the local pipeline."""

    def choose_action(self, **kwargs) -> Action:
        print("Enter one action JSON (click, drag, or done):")
        return parse_action(input("> "))


class ReplayProvider:
    """Read deterministic actions from a JSONL file, one action per step."""

    def __init__(self, path: Path):
        self._actions = [
            parse_action(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self._cursor = 0

    def choose_action(self, **kwargs) -> Action:
        if self._cursor >= len(self._actions):
            return parse_action(
                {"type": "done", "reason": "replay action list exhausted"}
            )
        action = self._actions[self._cursor]
        self._cursor += 1
        return action
