# Twelve Data Capture Blocker (2026-07-14)

The configured 1Password reference was tested at runtime without writing its
value to the repository or logs:

```sh
op read 'op://Cloud Web/hnvqyxxlakwyl7qhwmdosqy5j4/TWELVEDATA_API_SECRET'
```

The command failed with:

```text
could not get item Cloud Web/hnvqyxxlakwyl7qhwmdosqy5j4: "Cloud Web" isn't a vault in this account. Specify the vault with its ID or name.
```

No Twelve Data request was made and no secret was persisted. To resume, the
operator must provide a valid vault/item reference for this account, then run
the capture with runtime-only injection, for example:

```sh
TWELVEDATA_API_KEY="$(op read 'op://<vault>/<item>/TWELVEDATA_API_SECRET')" \
  uv run python scripts/capture_twelve_data_prices.py --symbols DDOG,DUOL,NICE,SHOP
```

The capture must write only redacted response metadata, content hashes, and
field-level comparison rows; never the API key or raw authenticated response.
