# Contributing to pywrkr

Thank you for your interest in contributing to pywrkr! This guide will help you get started.

## Getting Started

### Prerequisites

- Python 3.10 or newer
- Git

### Development Setup

```bash
# Clone the repository
git clone https://github.com/kurok/pywrkr.git
cd pywrkr

# Install in development mode with all extras
pip install -e ".[dev,otel,tui]"

# Verify everything works
pytest
```

### Project Structure

```
pywrkr/
  src/pywrkr/
    __init__.py       # Public API and version
    main.py           # All implementation code
  tests/
    test_pywrkr.py    # Unit and integration tests
  .github/workflows/  # CI/CD pipelines
```

## How to Contribute

### Reporting Bugs

1. Check existing [issues](https://github.com/kurok/pywrkr/issues) to avoid duplicates
2. Open a new issue using the **Bug Report** template
3. Include your Python version, OS, and steps to reproduce

### Suggesting Features

1. Check existing [issues](https://github.com/kurok/pywrkr/issues) for similar ideas
2. Open a new issue using the **Feature Request** template
3. Describe the use case and expected behavior

### Submitting Code

1. **Fork** the repository
2. **Create a branch** from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
3. **Make your changes** — keep commits focused and atomic
4. **Run the tests** and make sure they all pass:
   ```bash
   pytest
   ```
5. **Run the linter**:
   ```bash
   flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
   ```
6. **Push** your branch and open a **Pull Request**

## Code Guidelines

### Style

- Follow existing code patterns in the project
- Keep functions focused — one function, one job
- Use type hints for function signatures
- No unnecessary abstractions — simple and direct is better

### Testing

- Add tests for new features
- Tests run in parallel (pytest-xdist), so ensure your tests don't depend on shared state or fixed ports
- Integration tests use `AioHTTPTestCase` which assigns random ports automatically
- Run the full suite before submitting: `pytest`

### Commit Messages

- Use clear, descriptive commit messages
- Start with a verb: "Add ...", "Fix ...", "Update ..."
- Keep the first line under 72 characters

### Pull Requests

- Reference any related issues (e.g., "Fixes #42")
- Describe what changes and why
- Keep PRs focused — one feature or fix per PR
- Make sure CI is green before requesting review

## Running Tests

```bash
# Run all tests (parallel, default)
pytest

# Run a specific test class
pytest tests/test_pywrkr.py::TestTrafficProfiles -v

# Run sequentially (for debugging)
pytest -n 0 -v

# Run with coverage (if installed)
pytest --cov=pywrkr
```

## Release Process

Releases are managed by the maintainers:

1. Version is bumped in `pyproject.toml` and `src/pywrkr/__init__.py`
2. A GitHub Release is created with a tag (e.g., `v1.0.2`)
3. PyPI publish and Docker image build trigger automatically via GitHub Actions

## Questions?

Open a [discussion](https://github.com/kurok/pywrkr/issues) or reach out via an issue. We're happy to help!
