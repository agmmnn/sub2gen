# Extension core package

Typed, browser-independent primitives shared by sub2gen extensions:

- callback-based extension storage adapters;
- JSON HTTP request handling;
- WebSocket URL normalization and REST-base derivation.

Worker modes, account synchronization, UI state, and provider behavior remain
inside their owning extension. This package intentionally contains no Chrome
globals or product-specific state machines.
