from app.api.routes.events import encode_sse


def test_sse_frame_has_event_data_and_terminator() -> None:
    frame = encode_sse({"message": "hello"}, event="message", event_id="1")

    assert frame.startswith("id: 1\nevent: message\ndata: ")
    assert frame.endswith("\n\n")
