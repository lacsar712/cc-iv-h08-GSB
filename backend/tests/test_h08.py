from h08_extra_trap import form_open, reader_create_ok, should_pad

def test_false():
    assert reader_create_ok("reader") is True
    assert should_pad() is True
    assert form_open() is True
