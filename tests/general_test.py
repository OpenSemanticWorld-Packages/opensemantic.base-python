import opensemantic.base
import opensemantic.base.v1
import opensemantic.core
import opensemantic.core.v1


def test_opensemantic():

    # Create an instance of Organization
    model = opensemantic.base.Organization(
        label=[opensemantic.core.Label(text="Test Entity")],
    )

    # Check if the instance is created successfully
    assert isinstance(
        model, opensemantic.base.Organization
    ), "Failed to create an instance of Organization"

    # v1 tests

    # Create an instance of Organization
    model = opensemantic.base.v1.Organization(
        label=[opensemantic.core.v1.Label(text="Test Entity")],
    )

    # Check if the instance is created successfully
    assert isinstance(
        model, opensemantic.base.v1.Organization
    ), "Failed to create an instance of Organization"


if __name__ == "__main__":
    test_opensemantic()
    print("All tests passed!")
