"""
What a certificate shows. Rendered from the issued Certificate row and its
lines only: every value on the page was snapshotted at issue, never read
from live tables.
"""
from django.template.loader import render_to_string


def certificate_html(certificate):
    return render_to_string(
        "certificates/certificate.html",
        {
            "c": certificate,
            "lines": certificate.lines.order_by("event_date"),
            "statement": certificate.accreditation_statement,
        },
    )
