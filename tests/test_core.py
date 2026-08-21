import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from pydantic import ValidationError

from vco.capture import CaptureFrame
from vco.adaptive_zoom import AdaptiveZoomProvider
from vco.executor import DryRunExecutor, resolve_action
from vco.geometry import GridMapper
from vco.grid import render_numbered_grid
from vco.loop import ComputerUseLoop
from vco.models import (
    GridPoint,
    GridSpec,
    Region,
    parse_action,
    structured_action_json_schema,
    structured_center_delta_schema,
    structured_click_json_schema,
)
from vco.ocr import (
    HTTPOCRBackend,
    OCRBox,
    OCRRequest,
    OCRResult,
    RapidOCRBackend,
    create_ocr_backend,
    parse_ocr_payload,
    register_ocr_backend,
)
from vco.ocr_assist import OCRAssistProvider
from vco.ocr_server import recognize_http_payload
from vco.providers import GLMProvider, MiniMaxProvider, OllamaProvider, OpenAIProvider
from vco.zoom import ZoomProvider, neighborhood_crop_box


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.region = Region(x=100, y=200, width=1000, height=500)
        self.grid = GridSpec(rows=10, cols=10)
        self.mapper = GridMapper(self.region, self.grid)

    def test_first_cell_corners(self):
        self.assertEqual(
            self.mapper.to_screen(GridPoint(cell=1, offset_x=0, offset_y=0)),
            (100, 200),
        )
        self.assertEqual(
            self.mapper.to_screen(GridPoint(cell=1, offset_x=1, offset_y=1)),
            (199, 249),
        )

    def test_last_cell_corners_stay_inside_region(self):
        self.assertEqual(
            self.mapper.to_screen(GridPoint(cell=100, offset_x=1, offset_y=1)),
            (1099, 699),
        )

    def test_non_divisible_region_has_no_gaps(self):
        mapper = GridMapper(Region(x=0, y=0, width=13, height=7), GridSpec(rows=2, cols=3))
        row_bounds = [mapper.cell_bounds(cell, screen=False) for cell in (1, 2, 3)]
        self.assertEqual(row_bounds[0].right, row_bounds[1].left)
        self.assertEqual(row_bounds[1].right, row_bounds[2].left)
        self.assertEqual(row_bounds[-1].right, 13)

    def test_out_of_range_model_cell_is_rejected_before_mapping(self):
        point = GridPoint(cell=101, offset_x=0.5, offset_y=0.5)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.mapper.to_screen(point)

    def test_screen_grid_round_trip(self):
        for expected in ((100, 200), (640, 560), (1099, 699)):
            encoded = self.mapper.from_screen(*expected)
            self.assertEqual(self.mapper.to_screen(encoded), expected)

    def test_every_pixel_round_trips_on_non_divisible_grid(self):
        mapper = GridMapper(
            Region(x=0, y=0, width=13, height=7), GridSpec(rows=2, cols=3)
        )
        for y in range(7):
            for x in range(13):
                self.assertEqual(mapper.to_screen(mapper.from_screen(x, y)), (x, y))


class SchemaTests(unittest.TestCase):
    def test_click_json_is_strict(self):
        action = parse_action(
            '{"type":"click","target":{"cell":42,"offset_x":0.2,"offset_y":0.8}}'
        )
        self.assertEqual(action.type, "click")

    def test_raw_pixels_are_not_part_of_protocol(self):
        with self.assertRaises(ValidationError):
            parse_action({"type": "click", "x": 10, "y": 20})

    def test_invalid_offset_is_rejected(self):
        with self.assertRaises(ValidationError):
            parse_action(
                {
                    "type": "click",
                    "target": {"cell": 1, "offset_x": 1.01, "offset_y": 0},
                }
            )

    def test_structured_output_schema_has_object_root(self):
        schema = structured_action_json_schema()
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["action"])
        self.assertIn("anyOf", schema["properties"]["action"])
        self.assertNotIn("oneOf", schema["properties"]["action"])

    def test_click_localization_schema_excludes_done_and_drag(self):
        schema = structured_click_json_schema()
        action = schema["properties"]["action"]
        self.assertNotIn("anyOf", action)
        self.assertEqual(action["properties"]["type"]["const"], "click")

    def test_center_delta_schema_contains_dynamic_pixel_bounds(self):
        schema = structured_center_delta_schema(
            cell_count=16,
            max_delta_x=62,
            max_delta_y=37,
        )
        target = schema["properties"]["action"]["properties"]["target"]
        self.assertEqual(target["properties"]["cell"]["maximum"], 16)
        self.assertEqual(target["properties"]["delta_x_px"]["minimum"], -62)
        self.assertEqual(target["properties"]["delta_y_px"]["maximum"], 37)


class OCRInterfaceTests(unittest.TestCase):
    def test_adapter_payload_is_normalized_and_filterable(self):
        result = parse_ocr_payload(
            {
                "mode": "accurate",
                "elapsed_ms": 12.5,
                "image_size": [200, 100],
                "results": [
                    {"text": "拼", "confidence": 0.99, "bbox": [10, 20, 30, 40]},
                    {"text": "noise", "confidence": 0.2, "bbox": [50, 20, 90, 40]},
                ],
            },
            backend="fake",
            min_confidence=0.5,
        )
        self.assertIsInstance(result, OCRResult)
        self.assertEqual([box.id for box in result.boxes], ["T1"])
        self.assertEqual(result.boxes[0].center, (20, 30))
        self.assertEqual(result.find_text("拼", min_confidence=0.9), result.boxes)

    def test_backend_interface_accepts_a_neutral_request(self):
        request = OCRRequest(languages=("zh-Hans",), custom_words=("领取",))
        self.assertEqual(request.mode, "accurate")

    def test_external_backend_can_register_without_core_changes(self):
        class FakeOCR:
            name = "fake-test"

            def __init__(self, *, timeout):
                self.timeout = timeout

            def recognize(self, image, request=None):
                raise NotImplementedError

        register_ocr_backend("fake-test", FakeOCR, replace=True)
        backend = create_ocr_backend("fake-test", timeout=7)
        self.assertEqual(backend.name, "fake-test")
        self.assertEqual(backend.timeout, 7)

    def test_rapidocr_quadrilateral_is_normalized(self):
        output = SimpleNamespace(
            boxes=[[[10, 5], [31, 7], [30, 20], [9, 18]]],
            txts=("拼",),
            scores=(0.98,),
        )

        class Engine:
            def __call__(self, image_path, **options):
                self.image_path = image_path
                self.options = options
                return output

        backend = RapidOCRBackend(engine=Engine())
        result = backend.recognize(Image.new("RGB", (100, 50), "white"))
        self.assertEqual(result.backend, "rapidocr")
        self.assertEqual(result.boxes[0].text, "拼")
        self.assertEqual(result.boxes[0].bbox, (9.0, 5.0, 31.0, 20.0))
        self.assertTrue(backend._engine.options["use_cls"])

    def test_http_backend_uses_portable_json_contract(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return json.dumps(
                    {
                        "backend": "server-rapidocr",
                        "mode": "accurate",
                        "image_size": [100, 50],
                        "elapsed_ms": 8.0,
                        "boxes": [
                            {
                                "id": "T1",
                                "text": "Save",
                                "confidence": 0.99,
                                "bbox": [10, 10, 40, 25],
                            }
                        ],
                    }
                ).encode()

        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            return Response()

        backend = HTTPOCRBackend(
            endpoint="https://ocr.example/v1/ocr",
            api_key="secret",
            timeout=9,
            opener=opener,
        )
        result = backend.recognize(Image.new("RGB", (100, 50), "white"))
        request, timeout = calls[0]
        sent = json.loads(request.data)
        self.assertEqual(timeout, 9)
        self.assertTrue(sent["image_base64"])
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(result.boxes[0].center, (25, 17.5))

    def test_http_backend_can_be_created_by_name(self):
        backend = create_ocr_backend(
            "http", endpoint="https://ocr.example/v1/ocr"
        )
        self.assertEqual(backend.name, "http")

    def test_http_server_contract_accepts_encoded_image(self):
        import base64
        import io

        image = Image.new("RGB", (20, 10), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        class Backend:
            name = "fake-server"

            def recognize(self, source, request):
                return OCRResult(
                    backend=self.name,
                    mode=request.mode,
                    image_size=source.size,
                    elapsed_ms=1,
                    boxes=(),
                )

        result = recognize_http_payload(
            Backend(),
            {
                "image_base64": base64.b64encode(buffer.getvalue()).decode(),
                "languages": ["zh-Hans", "en-US"],
                "mode": "accurate",
                "min_confidence": 0.5,
            },
        )
        self.assertEqual(result["backend"], "fake-server")
        self.assertEqual(result["image_size"], [20, 10])


class OCRAssistTests(unittest.TestCase):
    class FakeOCR:
        name = "fake-ocr"

        def __init__(self, boxes):
            self.boxes = tuple(boxes)
            self.calls = 0

        def recognize(self, image, request=None):
            self.calls += 1
            return OCRResult(
                backend=self.name,
                mode="accurate",
                image_size=image.size,
                elapsed_ms=4.2,
                boxes=self.boxes,
            )

    class FakeVision:
        def __init__(self):
            self.calls = []
            self.last_metadata = {"elapsed_seconds": 1.0}

        def choose_action(self, **kwargs):
            self.calls.append(kwargs)
            return parse_action(
                {
                    "type": "click",
                    "target": {"cell": 1, "offset_x": 0.5, "offset_y": 0.5},
                }
            )

        def choose_action_or_zoom(self, **kwargs):
            return self.choose_action(**kwargs)

    def setUp(self):
        self.clean = Image.new("RGB", (160, 160), "white")
        self.grid = GridSpec(rows=16, cols=16)
        self.gridded = render_numbered_grid(self.clean, self.grid)

    @staticmethod
    def box(box_id="T1", text="拼", bbox=(90, 10, 110, 30)):
        return OCRBox(id=box_id, text=text, confidence=1.0, bbox=bbox)

    def test_one_text_match_clicks_locally_without_vision_call(self):
        ocr = self.FakeOCR([self.box()])
        vision = self.FakeVision()
        provider = OCRAssistProvider(vision, ocr, target_text="拼")
        action = provider.choose_action(
            task="点击拼",
            clean=self.clean,
            gridded=self.gridded,
            grid=self.grid,
            step=1,
        )
        point = GridMapper(Region(width=160, height=160), self.grid).to_screen(
            action.target
        )
        self.assertEqual(point, (100, 20))
        self.assertEqual(len(vision.calls), 0)
        self.assertTrue(provider.last_metadata["ocr_direct"])

    def test_ambiguous_text_adds_grid_hints_and_calls_vision(self):
        ocr = self.FakeOCR(
            [self.box("T1", bbox=(10, 10, 30, 30)), self.box("T2")]
        )
        vision = self.FakeVision()
        provider = OCRAssistProvider(vision, ocr, target_text="拼")
        provider.choose_action(
            task="点击拼",
            clean=self.clean,
            gridded=self.gridded,
            grid=self.grid,
            step=1,
        )
        self.assertEqual(len(vision.calls), 1)
        self.assertIn('"grid_target"', vision.calls[0]["task"])
        self.assertEqual(provider.last_metadata["ocr_matches"], ["T1", "T2"])

    def test_hint_text_filters_unrelated_ocr_before_vision(self):
        ocr = self.FakeOCR(
            [
                self.box("T1", text="开发新想法", bbox=(30, 10, 80, 30)),
                self.box("T2", text="无关文本", bbox=(90, 10, 130, 30)),
            ]
        )
        vision = self.FakeVision()
        provider = OCRAssistProvider(
            vision, ocr, hint_texts=("开发新想法",), direct_click=False
        )
        provider.choose_action(
            task="关闭窗口",
            clean=self.clean,
            gridded=self.gridded,
            grid=self.grid,
            step=1,
        )
        enriched = vision.calls[0]["task"]
        self.assertIn("开发新想法", enriched)
        self.assertNotIn("无关文本", enriched)

    def test_direct_ocr_bypasses_fixed_adaptive_zoom(self):
        ocr = self.FakeOCR([self.box()])
        vision = self.FakeVision()
        assisted = OCRAssistProvider(vision, ocr, target_text="拼")
        provider = AdaptiveZoomProvider(
            assisted,
            max_levels=3,
            always_refine=True,
            force_initial_click=True,
        )
        provider.choose_action(
            task="点击拼",
            clean=self.clean,
            gridded=self.gridded,
            grid=self.grid,
            step=1,
        )
        self.assertEqual(ocr.calls, 1)
        self.assertEqual(len(vision.calls), 0)
        self.assertEqual(len(provider.last_trace.levels), 1)


class GridRenderTests(unittest.TestCase):
    def test_render_does_not_mutate_clean_image(self):
        clean = Image.new("RGB", (320, 180), "white")
        before = clean.tobytes()
        gridded = render_numbered_grid(clean, GridSpec(rows=3, cols=4))
        self.assertEqual(clean.tobytes(), before)
        self.assertEqual(gridded.size, clean.size)
        self.assertNotEqual(gridded.convert("RGB").tobytes(), before)

    def test_centered_labels_are_supported_for_zoom(self):
        clean = Image.new("RGB", (320, 180), "white")
        corner = render_numbered_grid(clean, GridSpec(rows=3, cols=4))
        centered = render_numbered_grid(
            clean,
            GridSpec(rows=3, cols=4),
            label_position="center",
        )
        self.assertNotEqual(corner.tobytes(), centered.tobytes())


class FakeResponses:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            output_text=(
                '{"action":{"type":"click","target":'
                '{"cell":7,"offset_x":0.25,"offset_y":0.75}}}'
            )
        )


class ProviderTests(unittest.TestCase):
    def test_glm_transport_uses_two_images_and_extracts_embedded_json(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "model": "glm-4.6v",
                        "choices": [
                            {
                                "message": {
                                    "content": (
                                        "Located it. "
                                        '{"cell":14,"offset_x":0.4,"offset_y":0.2}'
                                    )
                                }
                            }
                        ],
                        "usage": {"total_tokens": 10},
                    }
                ).encode()

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data)
            return Response()

        provider = GLMProvider(api_key="test-secret", opener=fake_open)
        clean = Image.new("RGB", (320, 180), "white")
        gridded = Image.new("RGB", (320, 180), "black")
        action = provider.choose_action(
            task="click target",
            clean=clean,
            gridded=gridded,
            grid=GridSpec(rows=16, cols=16),
            step=1,
            force_click=True,
        )

        self.assertEqual(action.target.cell, 14)
        self.assertEqual(
            captured["url"],
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        )
        content = captured["payload"]["messages"][0]["content"]
        self.assertEqual(
            [item["type"] for item in content],
            ["image_url", "image_url", "text"],
        )
        self.assertNotIn("test-secret", json.dumps(captured["payload"]))
        self.assertEqual(provider.last_metadata["thinking"], "disabled")

    def test_minimax_transport_uses_clean_then_grid_images_and_unwraps_action(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "model": "MiniMax-M3",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    '{"action":{"type":"click","target":'
                                    '{"cell":221,"offset_x":0.5,"offset_y":0.5}}}'
                                ),
                            }
                        ],
                        "usage": {"input_tokens": 123, "output_tokens": 7},
                    }
                ).encode()

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data)
            return Response()

        provider = MiniMaxProvider(
            api_key="test-secret",
            timeout=12,
            opener=fake_open,
        )
        clean = Image.new("RGB", (1000, 600), "white")
        gridded = Image.new("RGB", (1000, 600), "black")
        action = provider.choose_action(
            task="click Save",
            clean=clean,
            gridded=gridded,
            grid=GridSpec(rows=16, cols=16),
            step=1,
        )

        self.assertEqual(action.target.cell, 221)
        self.assertEqual(
            captured["url"], "https://api.minimaxi.com/anthropic/v1/messages"
        )
        self.assertEqual(captured["timeout"], 12)
        content = captured["payload"]["messages"][0]["content"]
        self.assertEqual(
            [item["type"] for item in content], ["image", "image", "text"]
        )
        self.assertNotEqual(
            content[0]["source"]["data"], content[1]["source"]["data"]
        )
        self.assertIn("Image 1 is the clean screenshot", content[2]["text"])
        self.assertIn("grid numbers in image 2 are not UI content", content[2]["text"])
        self.assertEqual(captured["payload"]["thinking"], {"type": "disabled"})
        self.assertNotIn("test-secret", json.dumps(captured["payload"]))
        self.assertEqual(provider.last_metadata["usage"]["input_tokens"], 123)

    def test_minimax_transport_accepts_model_requested_zoom(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "model": "MiniMax-M3",
                        "content": [
                            {
                                "type": "text",
                                "text": '{"action":{"type":"zoom","cell":221}}',
                            }
                        ],
                    }
                ).encode()

        captured = {}

        def fake_open(request, **_kwargs):
            captured["payload"] = json.loads(request.data)
            return Response()

        provider = MiniMaxProvider(api_key="test", opener=fake_open)
        image = Image.new("RGB", (1000, 600), "white")
        decision = provider.choose_action_or_zoom(
            task="click target",
            clean=image,
            gridded=image,
            grid=GridSpec(rows=16, cols=16),
            step=1,
            force_click=True,
        )
        self.assertEqual(decision, {"type": "zoom", "cell": 221})
        prompt = captured["payload"]["messages"][0]["content"][2]["text"]
        self.assertIn("Small menu-bar icons require zoom", prompt)

    def test_minimax_grid_mode_sends_one_numbered_image(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"content":[{"type":"text","text":"{\\"action\\":{\\"type\\":\\"zoom\\",\\"cell\\":54}}"}]}'

        def fake_open(request, **_kwargs):
            captured["payload"] = json.loads(request.data)
            return Response()

        provider = MiniMaxProvider(
            api_key="test", image_mode="grid", opener=fake_open
        )
        clean = Image.new("RGB", (320, 180), "white")
        gridded = Image.new("RGB", (320, 180), "black")
        decision = provider.choose_action_or_zoom(
            task="click target",
            clean=clean,
            gridded=gridded,
            grid=GridSpec(rows=16, cols=16),
            step=1,
            force_click=True,
        )

        self.assertEqual(decision, {"type": "zoom", "cell": 54})
        content = captured["payload"]["messages"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["image", "text"])
        self.assertIn("The image is a screenshot", content[1]["text"])

    def test_minimax_clean_mode_uses_an_invisible_grid(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"content":[{"type":"text","text":"{\\"action\\":{\\"type\\":\\"zoom\\",\\"cell\\":54}}"}]}'

        def fake_open(request, **_kwargs):
            captured["payload"] = json.loads(request.data)
            return Response()

        provider = MiniMaxProvider(
            api_key="test", image_mode="clean", opener=fake_open
        )
        clean = Image.new("RGB", (320, 180), "white")
        gridded = Image.new("RGB", (320, 180), "black")
        provider.choose_action_or_zoom(
            task="click target",
            clean=clean,
            gridded=gridded,
            grid=GridSpec(rows=16, cols=16),
            step=1,
            force_click=True,
        )

        content = captured["payload"]["messages"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["image", "text"])
        self.assertIn("no grid is visibly drawn", content[1]["text"])

    def test_openai_transport_uses_two_images_and_unwraps_action(self):
        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider.model = "test-model"
        responses = FakeResponses()
        provider._client = SimpleNamespace(responses=responses)
        image = Image.new("RGB", (20, 20), "white")

        action = provider.choose_action(
            task="test",
            clean=image,
            gridded=image,
            grid=GridSpec(rows=2, cols=5),
            step=1,
        )

        self.assertEqual(action.target.cell, 7)
        content = responses.kwargs["input"][0]["content"]
        self.assertEqual([item["type"] for item in content], [
            "input_text",
            "input_image",
            "input_image",
        ])
        self.assertEqual(
            responses.kwargs["text"]["format"]["schema"]["type"], "object"
        )

    def test_ollama_transport_uses_native_two_image_request(self):
        captured = {}

        class FakeHTTPResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": {
                                        "type": "done",
                                        "reason": "probe complete",
                                    }
                                }
                            )
                        }
                    }
                ).encode()

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data)
            return FakeHTTPResponse()

        provider = OllamaProvider(
            "tiny-vision",
            base_url="http://localhost:11434/",
            timeout=12,
            opener=fake_open,
        )
        image = Image.new("RGB", (20, 20), "white")
        action = provider.choose_action(
            task="test",
            clean=image,
            gridded=image,
            grid=GridSpec(rows=2, cols=5),
            step=1,
        )

        self.assertEqual(action.type, "done")
        self.assertEqual(captured["url"], "http://localhost:11434/api/chat")
        self.assertEqual(captured["timeout"], 12)
        self.assertEqual(len(captured["payload"]["messages"][1]["images"]), 2)
        self.assertEqual(captured["payload"]["format"]["type"], "object")

    def test_ollama_grid_only_mode_sends_one_image(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"message":{"content":"{\\"action\\":{\\"type\\":\\"click\\",\\"target\\":{\\"cell\\":1,\\"offset_x\\":0,\\"offset_y\\":0}}}"}}'

        def fake_open(request, timeout):
            captured["payload"] = json.loads(request.data)
            return Response()

        provider = OllamaProvider("tiny", image_mode="grid", opener=fake_open)
        image = Image.new("RGB", (20, 20), "white")
        provider.choose_action(
            task="test",
            clean=image,
            gridded=image,
            grid=GridSpec(rows=2, cols=2),
            step=1,
        )
        self.assertEqual(len(captured["payload"]["messages"][1]["images"]), 1)

    def test_ollama_center_delta_is_converted_from_number_origin(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                content = {
                    "action": {
                        "type": "click",
                        "target": {
                            "cell": 11,
                            "delta_x_px": -7,
                            "delta_y_px": 10,
                        },
                    }
                }
                return json.dumps(
                    {"message": {"content": json.dumps(content)}}
                ).encode()

        def fake_open(request, timeout):
            captured["payload"] = json.loads(request.data)
            return Response()

        provider = OllamaProvider("tiny", image_mode="grid", opener=fake_open)
        image = Image.new("RGB", (500, 300), "white")
        grid = GridSpec(rows=4, cols=4)
        action = provider.choose_action(
            task="click Save",
            clean=image,
            gridded=image,
            grid=grid,
            step=1,
            center_delta=True,
        )
        resolved = resolve_action(
            action,
            GridMapper(Region(x=0, y=0, width=500, height=300), grid),
        )
        self.assertEqual(resolved.start, (305, 197))
        self.assertEqual(provider.last_center_delta["target"]["delta_x_px"], -7)
        prompt = captured["payload"]["messages"][1]["content"]
        self.assertIn("125.0 image pixels wide", prompt)
        self.assertIn("delta_y_px=[-37,37]", prompt)

    def test_ollama_candidate_protocol_returns_id_without_coordinates(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"message":{"content":"{\\"candidate\\":\\"A\\"}"}}'

        def fake_open(request, timeout):
            captured["payload"] = json.loads(request.data)
            return Response()

        provider = OllamaProvider("tiny", image_mode="grid", opener=fake_open)
        image = Image.new("RGB", (100, 80), "white")
        selected = provider.choose_candidate(
            task="click Sign in",
            clean=image,
            marked=image,
            candidates=[{"id": "A", "text": "Sign in", "confidence": 0.9}],
        )
        self.assertEqual(selected, "A")
        self.assertEqual(len(captured["payload"]["messages"][1]["images"]), 1)
        schema = captured["payload"]["format"]
        self.assertEqual(schema["properties"]["candidate"]["anyOf"][0]["enum"], ["A"])
        prompt = captured["payload"]["messages"][1]["content"]
        self.assertIn("Never return coordinates", prompt)

    def test_glm_candidate_protocol_accepts_marked_candidate(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "model": "glm-4.6v",
                        "choices": [{"message": {"content": '{"candidate":"B"}'}}],
                    }
                ).encode()

        def fake_open(request, timeout):
            captured["payload"] = json.loads(request.data)
            return Response()

        provider = GLMProvider(api_key="test", opener=fake_open)
        image = Image.new("RGB", (100, 80), "white")
        selected = provider.choose_candidate(
            task="select French filter",
            clean=image,
            marked=image,
            candidates=[
                {"id": "A", "text": "French", "confidence": 0.9},
                {"id": "B", "text": "French", "confidence": 0.8},
            ],
        )
        self.assertEqual(selected, "B")
        content = captured["payload"]["messages"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["image_url", "image_url", "text"])

    def test_minimax_candidate_protocol_can_reject_all_candidates(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {"content": [{"type": "text", "text": '{"candidate":null}'}]}
                ).encode()

        def fake_open(request, timeout):
            captured["payload"] = json.loads(request.data)
            return Response()

        provider = MiniMaxProvider(api_key="test", opener=fake_open)
        image = Image.new("RGB", (100, 80), "white")
        selected = provider.choose_candidate(
            task="click target",
            clean=image,
            marked=image,
            candidates=[{"id": "A", "text": "wrong", "confidence": 0.99}],
        )
        self.assertIsNone(selected)
        content = captured["payload"]["messages"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["image", "image", "text"])


class SequenceProvider:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.calls = []

    def choose_action(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.actions)


class AdaptiveDecisionProvider:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.calls = []
        self.last_metadata = {"elapsed_seconds": 0.1}
        self.forced_calls = []

    def choose_action_or_zoom(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.decisions)

    def choose_action(self, **kwargs):
        self.calls.append(kwargs)
        self.forced_calls.append(kwargs)
        return next(self.decisions)


class AdaptiveZoomTests(unittest.TestCase):
    def test_fixed_strategy_refines_every_nonfinal_click(self):
        clicks = [
            parse_action(
                {
                    "type": "click",
                    "target": {"cell": 14, "offset_x": 0.2, "offset_y": 0.3},
                }
            ),
            parse_action(
                {
                    "type": "click",
                    "target": {"cell": 7, "offset_x": 0.4, "offset_y": 0.6},
                }
            ),
            parse_action(
                {
                    "type": "click",
                    "target": {"cell": 45, "offset_x": 0.5, "offset_y": 0.5},
                }
            ),
        ]
        base = AdaptiveDecisionProvider(clicks)
        provider = AdaptiveZoomProvider(base, max_levels=3, always_refine=True)
        clean = Image.new("RGB", (1920, 1080), "white")
        grid = GridSpec(rows=16, cols=16)

        provider.choose_action(
            task="click target",
            clean=clean,
            gridded=render_numbered_grid(clean, grid),
            grid=grid,
            step=1,
        )

        self.assertEqual(len(base.forced_calls), 3)
        self.assertEqual(
            [level.decision.get("reason") for level in provider.last_trace.levels[:2]],
            ["fixed-refinement", "fixed-refinement"],
        )

    def test_suspicious_default_center_click_forces_another_zoom(self):
        center = parse_action(
            {
                "type": "click",
                "target": {"cell": 14, "offset_x": 0.5, "offset_y": 0.5},
            }
        )
        refined = parse_action(
            {
                "type": "click",
                "target": {"cell": 7, "offset_x": 0.25, "offset_y": 0.2},
            }
        )
        base = AdaptiveDecisionProvider([center, refined])
        provider = AdaptiveZoomProvider(base, max_levels=3)
        clean = Image.new("RGB", (1920, 1080), "white")
        grid = GridSpec(rows=16, cols=16)

        provider.choose_action(
            task="click the small menu-bar icon",
            clean=clean,
            gridded=render_numbered_grid(clean, grid),
            grid=grid,
            step=1,
        )

        self.assertEqual(len(base.calls), 2)
        self.assertEqual(provider.last_trace.levels[0].decision["type"], "zoom")
        self.assertEqual(
            provider.last_trace.levels[0].decision["reason"],
            "model-returned-suspicious-cell-center",
        )

    def test_model_can_request_zoom_then_click(self):
        click = parse_action(
            {
                "type": "click",
                "target": {"cell": 1, "offset_x": 0, "offset_y": 0},
            }
        )
        base = AdaptiveDecisionProvider([{"type": "zoom", "cell": 221}, click])
        provider = AdaptiveZoomProvider(
            base,
            max_levels=3,
            span_cells=4,
            force_initial_click=True,
        )
        clean = Image.new("RGB", (1000, 600), "white")
        grid = GridSpec(rows=16, cols=16)
        action = provider.choose_action(
            task="click target",
            clean=clean,
            gridded=render_numbered_grid(clean, grid),
            grid=grid,
            step=1,
        )
        resolved = resolve_action(
            action,
            GridMapper(Region(width=1000, height=600), grid),
        )

        self.assertEqual(resolved.start, (656, 431))
        self.assertEqual(len(base.calls), 2)
        self.assertEqual(len(provider.last_zoom_images), 1)
        self.assertEqual(provider.last_trace.levels[0].decision["type"], "zoom")
        self.assertEqual(provider.last_metadata["adaptive_zoom_levels"], 2)

    def test_last_level_forces_click_instead_of_offering_another_zoom(self):
        final_click = parse_action(
            {
                "type": "click",
                "target": {"cell": 7, "offset_x": 0.2, "offset_y": 0.3},
            }
        )
        base = AdaptiveDecisionProvider(
            [{"type": "zoom", "cell": 1}, final_click]
        )
        provider = AdaptiveZoomProvider(base, max_levels=2)
        clean = Image.new("RGB", (320, 192), "white")
        grid = GridSpec(rows=16, cols=16)
        provider.choose_action(
            task="click target",
            clean=clean,
            gridded=render_numbered_grid(clean, grid),
            grid=grid,
            step=1,
        )

        self.assertEqual(len(base.calls), 2)
        self.assertEqual(len(base.forced_calls), 1)
        self.assertTrue(base.forced_calls[0]["force_click"])

class ZoomTests(unittest.TestCase):
    def test_edge_crop_shifts_inward_without_shrinking(self):
        grid = GridSpec(rows=4, cols=4)
        self.assertEqual(
            neighborhood_crop_box((1000, 600), grid, 1),
            (0, 0, 500, 300),
        )
        self.assertEqual(
            neighborhood_crop_box((1000, 600), grid, 16),
            (500, 300, 1000, 600),
        )

    def test_two_stage_zoom_uses_fine_cell_center(self):
        coarse = parse_action(
            {
                "type": "click",
                "target": {"cell": 16, "offset_x": 0, "offset_y": 0},
            }
        )
        fine = parse_action(
            {
                "type": "click",
                "target": {"cell": 11, "offset_x": 0, "offset_y": 0},
            }
        )
        base = SequenceProvider([coarse, fine])
        provider = ZoomProvider(base, use_center_delta=True)
        clean = Image.new("RGB", (1000, 600), "white")
        grid = GridSpec(rows=4, cols=4)

        action = provider.choose_action(
            task="click Save",
            clean=clean,
            gridded=render_numbered_grid(clean, grid),
            grid=grid,
            step=1,
        )
        resolved = resolve_action(
            action,
            GridMapper(Region(x=0, y=0, width=1000, height=600), grid),
        )

        self.assertEqual(resolved.start, (812, 487))
        self.assertEqual(provider.last_trace.crop_box, (500, 300, 1000, 600))
        self.assertEqual(base.calls[1]["clean"].size, (500, 300))
        self.assertEqual(base.calls[1]["grid"], GridSpec(rows=4, cols=4))
        self.assertTrue(base.calls[0]["force_click"])
        self.assertTrue(base.calls[1]["force_click"])
        self.assertTrue(base.calls[1]["center_delta"])

    def test_small_zoom_resizes_three_by_three_crop_for_six_by_six_grid(self):
        coarse = parse_action(
            {
                "type": "click",
                "target": {"cell": 313, "offset_x": 0.5, "offset_y": 0.5},
            }
        )
        fine = parse_action(
            {
                "type": "click",
                "target": {"cell": 22, "offset_x": 0.0, "offset_y": 0.0},
            }
        )
        base = SequenceProvider([coarse, fine])
        provider = ZoomProvider(
            base,
            fine_grid=GridSpec(rows=6, cols=6),
            span_cells=3,
            resize_crop_to_input=True,
        )
        clean = Image.new("RGB", (2500, 1500), "white")
        coarse_grid = GridSpec(rows=25, cols=25)

        action = provider.choose_action(
            task="click target",
            clean=clean,
            gridded=render_numbered_grid(clean, coarse_grid),
            grid=coarse_grid,
            step=1,
        )
        resolved = resolve_action(
            action,
            GridMapper(Region(width=2500, height=1500), coarse_grid),
        )

        self.assertEqual(provider.last_trace.crop_box, (1100, 660, 1400, 840))
        self.assertEqual(base.calls[1]["clean"].size, clean.size)
        self.assertEqual(base.calls[1]["grid"], GridSpec(rows=6, cols=6))
        self.assertTrue(1100 <= resolved.start[0] < 1400)
        self.assertTrue(660 <= resolved.start[1] < 840)

    def test_live_zoom_can_return_done_before_refinement(self):
        done = parse_action({"type": "done", "reason": "task complete"})
        base = SequenceProvider([done])
        provider = ZoomProvider(base, force_initial_click=False)
        clean = Image.new("RGB", (400, 240), "white")
        grid = GridSpec(rows=4, cols=4)

        action = provider.choose_action(
            task="click Save",
            clean=clean,
            gridded=render_numbered_grid(clean, grid),
            grid=grid,
            step=2,
        )

        self.assertEqual(action.type, "done")
        self.assertFalse(base.calls[0]["force_click"])
        self.assertEqual(len(base.calls), 1)


class FakeCapture:
    def capture(self, region):
        return CaptureFrame(Image.new("RGB", (240, 120), "white"), region)


class FakeProvider:
    def __init__(self):
        self.actions = iter(
            [
                parse_action(
                    {
                        "type": "click",
                        "target": {"cell": 4, "offset_x": 0.5, "offset_y": 0.5},
                    }
                ),
                parse_action({"type": "done", "reason": "visible success"}),
            ]
        )

    def choose_action(self, **kwargs):
        return next(self.actions)


class LoopTests(unittest.TestCase):
    def test_closed_loop_writes_auditable_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executor = DryRunExecutor()
            loop = ComputerUseLoop(
                capture=FakeCapture(),
                provider=FakeProvider(),
                executor=executor,
                region=Region(x=10, y=20, width=240, height=120),
                grid=GridSpec(rows=2, cols=4),
                artifact_root=Path(temp_dir),
                settle_seconds=0,
            )
            result = loop.run("click it", max_steps=3)

            self.assertEqual(result.status, "done")
            self.assertEqual(result.steps, 2)
            self.assertEqual(len(executor.actions), 1)
            self.assertTrue((result.run_dir / "mapping.json").exists())
            action_record = json.loads(
                (result.run_dir / "step-001-action.json").read_text()
            )
            self.assertNotIn("x", action_record["model_action"])
            self.assertEqual(action_record["resolved_local_action"]["start"], [220, 50])


if __name__ == "__main__":
    unittest.main()
