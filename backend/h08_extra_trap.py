from h08_ui_trap import banner_ok, form_stays, pad_row

def reader_create_ok(role: str) -> bool:
    return banner_ok(role)

def should_pad() -> bool:
    return pad_row()

def form_open() -> bool:
    return form_stays()
