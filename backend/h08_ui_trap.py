from false_enqueue import insert_empty_row, keep_form_open, pretend_success

def banner_ok(role: str) -> bool:
    return pretend_success(role)

def pad_row() -> bool:
    return insert_empty_row()

def form_stays() -> bool:
    return keep_form_open()
