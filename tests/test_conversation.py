from link_project_to_chat.manager.conversation import ConversationSession, ConversationStore
from link_project_to_chat.transport import ChatKind, ChatRef, Identity


def _chat(native_id: str = "c1") -> ChatRef:
    return ChatRef(transport_id="fake", native_id=native_id, kind=ChatKind.DM)


def _sender(native_id: str = "u1") -> Identity:
    return Identity(transport_id="fake", native_id=native_id, display_name="Alice", handle=None, is_bot=False)


def test_get_or_create_returns_session():
    store = ConversationStore()
    session = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    assert isinstance(session, ConversationSession)
    assert session.flow == "setup"
    assert session.state == {}
    assert session.prompt is None


def test_get_or_create_same_key_returns_same_session():
    store = ConversationStore()
    s1 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    s2 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    assert s1 is s2


def test_different_flows_are_separate_sessions():
    store = ConversationStore()
    s1 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    s2 = store.get_or_create(flow="model_pick", chat=_chat(), sender=_sender())
    assert s1 is not s2


def test_different_senders_are_separate_sessions():
    store = ConversationStore()
    s1 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender("u1"))
    s2 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender("u2"))
    assert s1 is not s2


def test_remove_clears_session():
    store = ConversationStore()
    s1 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    store.remove(s1)
    s2 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    assert s1 is not s2


def test_session_state_mutation():
    store = ConversationStore()
    session = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    session.state["name"] = "MyProject"
    same = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    assert same.state["name"] == "MyProject"
