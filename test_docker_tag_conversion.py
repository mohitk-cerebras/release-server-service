#!/usr/bin/env python3
"""Test docker tag conversion logic."""

def _construct_cbcore_image(app_tag: str) -> str:
    """Construct cbcore_image from app_tag.

    Docker image tags cannot contain '+' characters, so we convert PEP 440
    format back to Docker-compatible format.
    """
    registry = "171496337684.dkr.ecr.us-west-2.amazonaws.com/cbcore"

    # Convert PEP 440 format to Docker-compatible format
    docker_tag = app_tag

    # If tag contains '+' (PEP 440 format like "0.0.0+build.1b9c30c813")
    if "+" in app_tag:
        parts = app_tag.split("+", 1)
        local_part = parts[1]  # e.g., "build.1b9c30c813"
        # Check if this looks like a semantic version base (X.Y.Z+...)
        if parts[0] and parts[0][0].isdigit() and "." in parts[0]:
            # Replace dots with dashes in local part to get original format
            docker_tag = local_part.replace(".", "-")
        else:
            # Not a semantic version, use the whole tag with + replaced by -
            docker_tag = app_tag.replace("+", "-")
    # If tag starts with version (like "0.0.0-build-..."), extract suffix
    elif app_tag and app_tag[0].isdigit() and "-" in app_tag:
        parts = app_tag.split("-", 1)
        # Check if first part is a semantic version (X.Y.Z)
        base = parts[0]
        if base.count(".") >= 1:  # Looks like a version number
            # Use only the suffix after the version
            docker_tag = parts[1]

    return f"{registry}:{docker_tag}"


# Test cases
test_cases = [
    ("0.0.0+build.1b9c30c813", "build-1b9c30c813"),
    ("0.0.0-build-1b9c30c813", "build-1b9c30c813"),
    ("260113.3+inference.202602220136.2384.8fb6d540", "inference-202602220136-2384-8fb6d540"),
    ("260113.3-inference-202602220136-2384-8fb6d540", "inference-202602220136-2384-8fb6d540"),
    ("build-1b9c30c813", "build-1b9c30c813"),
    ("v2.3.0", "v2.3.0"),
    ("2.3.0-alpha-1", "alpha-1"),
]

print("Testing Docker tag conversion:")
print("=" * 80)

all_passed = True
for app_tag, expected_tag in test_cases:
    result = _construct_cbcore_image(app_tag)
    expected_image = f"171496337684.dkr.ecr.us-west-2.amazonaws.com/cbcore:{expected_tag}"

    if result == expected_image:
        status = "✅ PASS"
    else:
        status = "❌ FAIL"
        all_passed = False

    print(f"{status}: app_tag='{app_tag}'")
    print(f"  Expected: {expected_image}")
    print(f"  Got:      {result}")
    print()

print("=" * 80)
if all_passed:
    print("✅ All tests passed!")
else:
    print("❌ Some tests failed!")
