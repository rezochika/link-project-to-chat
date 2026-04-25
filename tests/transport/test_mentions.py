from link_project_to_chat.transport import ChatKind, ChatRef, FakeTransport, Identity, IncomingMessage


def _bot_id() -> Identity:
    return Identity(transport_id="fake", native_id="b1", display_name="Bot", handle="mybot", is_bot=True)


def _chat() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="r1", kind=ChatKind.ROOM)


def _sender() -> Identity:
    return Identity(transport_id="fake", native_id="u1", display_name="Alice", handle="alice", is_bot=False)


async def test_inject_message_carries_mentions():
    t = FakeTransport()
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    t.on_message(handler)
    await t.inject_message(_chat(), _sender(), "@mybot hello", mentions=[_bot_id()])

    assert len(received) == 1
    assert received[0].mentions == [_bot_id()]


async def test_inject_message_defaults_to_empty_mentions():
    t = FakeTransport()
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    t.on_message(handler)
    await t.inject_message(_chat(), _sender(), "hello")

    assert received[0].mentions == []
