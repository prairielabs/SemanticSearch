Semantic Search local configuration
===================================

The installer creates these local files after installation:

  mistral_api_key.txt
  mistral_model.txt

Do not place a real API key inside the distributable ZIP.

The default model file value is:

  mistral-medium-3-5

If Mistral publishes a different exact API model id for Medium 3.5, replace
the value in mistral_model.txt without changing the harness.
