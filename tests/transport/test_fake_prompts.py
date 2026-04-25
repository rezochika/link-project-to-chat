from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    FakeTransport,
    Identity,
    PromptKind,
    PromptRef,
    PromptSpec,
    PromptSubmission,
)


def _chat() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="c1", kind=ChatKind.DM)


def _sender() -> Identity:
    return Identity(transport_id="fake", native_id="u1", display_name="Alice", handle="alice", is_bot=False)


def _text_spec() -> PromptSpec:
    return PromptSpec(key="name", title="Your Name", body="Enter your name", kind=PromptKind.TEXT)


async def test_open_prompt_returns_prompt_ref():
    t = FakeTransport()
    ref = await t.open_prompt(_chat(), _text_spec())
    assert isinstance(ref, PromptRef)
    assert ref.key == "name"
    assert ref.chat == _chat()
    assert ref.transport_id == "fake"


async def test_open_prompt_recorded():
    t = FakeTransport()
    await t.open_prompt(_chat(), _text_spec())
    assert len(t.opened_prompts) == 1
    assert t.opened_prompts[0].spec.key == "name"


async def test_inject_prompt_submit_fires_handler():
    t = FakeTransport()
    submissions: list[PromptSubmission] = []

    async def handler(sub: PromptSubmission) -> None:
        submissions.append(sub)

    t.on_prompt_submit(handler)
    ref = await t.open_prompt(_chat(), _text_spec())
    await t.inject_prompt_submit(ref, _sender(), text="Alice")

    assert len(submissions) == 1
    assert submissions[0].text == "Alice"
    assert submissions[0].prompt == ref


async def test_close_prompt_recorded():
    t = FakeTransport()
    ref = await t.open_prompt(_chat(), _text_spec())
    await t.close_prompt(ref, final_text="Done!")
    assert len(t.closed_prompts) == 1
    assert t.closed_prompts[0].final_text == "Done!"
    assert t.closed_prompts[0].ref == ref


async def test_inject_prompt_submit_choice():
    from link_project_to_chat.transport import PromptOption, ButtonStyle

    spec = PromptSpec(
        key="pick",
        title="Pick",
        body="Choose one",
        kind=PromptKind.CHOICE,
        options=[PromptOption(value="a", label="A"), PromptOption(value="b", label="B")],
    )
    t = FakeTransport()
    seen: list[str | None] = []

    async def handler(sub: PromptSubmission) -> None:
        seen.append(sub.option)

    t.on_prompt_submit(handler)
    ref = await t.open_prompt(_chat(), spec)
    await t.inject_prompt_submit(ref, _sender(), option="b")

    assert seen == ["b"]
