# Meridian Execution Engine 2.0 -- Refactor Notes

The `release-2.0` branch rewrites parts of `src/portfolio_engine.py`:

* added a symbol-normalisation helper used by the order gateway;
* removed the legacy settlement shim comments;
* tightened coercion of the account book limit;
* removed the deprecated replay margin-buffer block;
* removed two audit markers whose underlying findings were remediated.
