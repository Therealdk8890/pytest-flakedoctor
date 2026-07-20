"""A deliberately flaky test to try the doctor on.

    PYTHONPATH=src python -m pytest examples/demo_flaky.py -p flakedoctor._plugin --doctor

The assertion depends on set iteration order, which depends on hash
randomization — it fails on roughly half of all interpreter starts. The
doctor pins the hash seed that makes it fail every time.
"""


def test_invoice_ids_stable():
    invoices = {"inv-apple", "inv-banana", "inv-cherry", "inv-date", "inv-elderberry", "inv-fig"}
    first = next(iter(invoices))
    assert first not in {"inv-apple", "inv-banana", "inv-cherry"}
