"""BYOC egress entries follow the same rules as a v1 job's network_policy (B5), and a trailing newline is refused."""
import pytest
from pydantic import ValidationError

from app.schemas.byoc import ByocNetwork


@pytest.mark.parametrize(
    "entry",
    ["localhost:5432", "169.254.169.254:80", "10.0.0.5:22", "metadata.google.internal:80", "foo.internal:80",
     "a.example:443\n", "a.example:0", "a.example:70000", "singlelabel:80"],
)
def test_bad_egress_refused(entry):
    with pytest.raises(ValidationError):
        ByocNetwork(egress=[entry])


def test_good_egress_normalised():
    assert ByocNetwork(egress=["B.Example:443", "b.example:443", "a.example:80"]).egress == ["a.example:80", "b.example:443"]


def test_trailing_newline_refused_in_spec_identifiers():
    from app.schemas.byoc import IMAGE_DIGEST_RE, ByocSpec
    from app.schemas.job_spec_v2 import InputRef, JobSpecV2

    digest = "sha256:" + "a" * 64
    good = f"registry.example/org/img@{digest}"
    assert IMAGE_DIGEST_RE.fullmatch(good)
    with pytest.raises(ValidationError):
        ByocSpec(version=2, mode="byoc", image=good + "\n")
    with pytest.raises(ValidationError):
        ByocSpec(version=2, mode="byoc", image=good, env={"FOO\n": "x"})
    with pytest.raises(ValidationError):
        JobSpecV2(version=2, kit="upscale@1.0.0\n")
    with pytest.raises(ValidationError):
        InputRef(name="a", source="dataset:abc\n")
