"""
Test-only stubs for the optional `google-genai` and `anthropic` SDKs.

These regression tests only care about *prompt construction* -- i.e. what
text app/extractor.py sends to the LLM client -- not about real network
calls. Neither SDK is installed in this environment, and extractor.py
already handles that gracefully (HAS_GEMINI_SDK / HAS_ANTHROPIC_SDK become
False and the extract_with_* functions short-circuit to None). To actually
exercise the prompt-construction code paths, we register minimal fake
modules in sys.modules *before* app.extractor is imported, so its
`import anthropic` / `from google import genai` succeed and HAS_*_SDK is
True. The tests then monkeypatch the client classes on the already-imported
`app.extractor` module to capture call arguments.
"""
import sys
import types


def _install_fake_sdks():
    if "anthropic" not in sys.modules:
        anthropic_mod = types.ModuleType("anthropic")
        anthropic_mod.Anthropic = object  # replaced per-test via monkeypatch
        sys.modules["anthropic"] = anthropic_mod

    if "google" not in sys.modules:
        sys.modules["google"] = types.ModuleType("google")
    if "google.genai" not in sys.modules:
        genai_mod = types.ModuleType("google.genai")
        genai_mod.Client = object  # replaced per-test via monkeypatch
        sys.modules["google.genai"] = genai_mod
        sys.modules["google"].genai = genai_mod
    if "google.genai.types" not in sys.modules:
        types_mod = types.ModuleType("google.genai.types")
        types_mod.GenerateContentConfig = lambda **kwargs: kwargs
        sys.modules["google.genai.types"] = types_mod
        sys.modules["google.genai"].types = types_mod


_install_fake_sdks()
