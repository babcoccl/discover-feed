"""Story Q&A: streamed, source-grounded, cited answers from a profile's ``llm.chat`` role.

``context`` builds the prompt input (numbered sources, brief, report, recent turns),
``prompt`` the versioned system prompt, ``service`` streams an answer (priority over the
generation worker, upstream cancellation), ``verify`` checks it afterwards, ``store`` keeps
conversations, and ``api`` exposes ``/api/v1/stories/{id}/ask`` and friends."""
