"""One persisted host selection for the desktop and web monitors."""


def hidden_hosts(store):
    stored = store.get_pref("host_monitor_hidden_hosts", [])
    return (
        {value for value in stored if isinstance(value, str)} if isinstance(stored, list) else set()
    )


def visible_hosts(store):
    hidden = hidden_hosts(store)
    return [host for host in store.host_list() if host.enabled and host.id not in hidden]
