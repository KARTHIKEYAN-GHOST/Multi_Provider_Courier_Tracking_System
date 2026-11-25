# Indian couriers: Delhivery, Safexpress, SkyKing, ABT, Balaji Roadlines

Five adapters added between Phase 8 and Phase 9. Each was written from what the courier
publishes or from what its public tracking page does. **No consignment was looked up on any
courier's system while writing them**, so none has been tried against the real service.

| Provider code | Courier | How it is tracked | Method | What it needs before use | Confidence |
|---|---|---|---|---|---|
| `delhivery` | Delhivery | Official "Order Tracking" API, `GET /api/v1/packages/json/?waybill=` | `api` | Delhivery API token | High: written from Delhivery's API description |
| `safexpress` | Safexpress | Customer waybill tracking API (OAuth2 client credentials + API key) | `api` | Client id, client secret, API key from Safexpress | Medium: request and answer outline confirmed, endpoint address not |
| `skyking` | SkyKing | The JSON service behind the page skyking.co/track | `scrape` | SkyKing's permission | Medium: request and field names read from the page; date format not seen |
| `abt` | ABT Parcel Service | The "Track Shipment" form on abtlogistics.com (`POST track_submit.php`) | `scrape` | ABT's permission, and ABT switching tracking back on | Low: result page never seen |
| `balaji` | Balaji Roadlines (Sri Balaji Transport Lines) | The "LR Tracking" page, `GET /Website/tracking?lrno=` | `scrape` | The company's permission | Low: result page never seen |

All five are in the database after migration `0013_indian_couriers.sql` and are listed by
`GET /v1/providers`, but the Engine refuses to create a job for any of them until its
requirement is met (`409 provider_unavailable`).

## Delhivery

- Source: Delhivery's developer documentation (delhivery-express-api-doc.readme.io, "Order Tracking API").
- Request: `GET https://track.delhivery.com/api/v1/packages/json/?waybill=<number>`; test system `https://staging-express.delhivery.com`.
- The token is sent as `Authorization: Token <token>`, not in the address. The reference page
  for this call describes a `token` query parameter; the header form is what Delhivery uses
  for its other calls. **Check this with the first real call**; if Delhivery rejects it, the
  change is one line in `adapters/delhivery/adapter.py`.
- Answer: `ShipmentData[].Shipment.Scans[].ScanDetail` with `ScanType`, `Scan`, `ScanDateTime`
  (IST), `ScannedLocation`, `Instructions`.
- Raw status: `<ScanType>:<Scan>`, for example `UD:In Transit` (on the way) and `RT:In Transit`
  (on the way back). Thirteen combinations are mapped; the list of scan texts is from general
  knowledge of Delhivery, not from the page that was read, so expect a few unmapped ones.
- Rate limit: Delhivery allows 750 calls per 5 minutes per address; the provider is set to at
  most one dispatch per 500 ms.

```
courier-engine admin set-credential --provider delhivery --label main     # then type {"token":"..."}
courier-engine admin create-worker-token --adapter delhivery             # -> DELHIVERY_WORKER_TOKEN
```

## Safexpress

- The public website cannot be automated: before showing a waybill it asks for a phone
  number, an OTP sent to it and a CAPTCHA. The website's own script also contains access
  data of Safexpress's internal services. **Those are Safexpress's, are not used here and
  must not be.**
- Safexpress gives account customers API access. It publishes no description; it sends one
  with the credentials.
- Confirmed (it is what Safexpress's website does): token address
  `https://api-auth.safexpress.com/oauth2/token`, request body `{"docNo": "<waybill>", "docType": "WB"}`,
  answer `shipment.result` (`"failed"` = unknown) and `shipment.tracking[]` with `eventdt`,
  `eventdesc`, `status`.
- **Not confirmed**: the tracking address, the OAuth scope and whether `Authorization` carries
  the bare token. The adapter uses commonly cited values. Compare them with Safexpress's
  document and set `SAFEXPRESS_TRACKING_URL`, `SAFEXPRESS_TOKEN_URL`, `SAFEXPRESS_SCOPE` if they differ.
- One lookup makes two calls (token, then tracking). The token is not kept.

```
courier-engine admin set-credential --provider safexpress --label main
#   then type {"client_id":"...","client_secret":"...","api_key":"..."}
courier-engine admin create-worker-token --adapter safexpress            # -> SAFEXPRESS_WORKER_TOKEN
```

## SkyKing

- SkyKing publishes no API. Its tracking page gets a consignment's history as JSON from
  `https://live.skyking.co` in two calls: one for the page's access key, one for the history
  (`/api/Track/ConsignmentMTrack_WebSite_AuthNew?cnote=`). The adapter repeats them.
- A lookup needs no CAPTCHA. (The reCAPTCHA on that page guards the "send me alerts" form.)
- Answer: a list of entries with `DateTime`, `Status`, `Location`; an empty list means unknown.
- This is use of a website's internal service. Ask SkyKing first; with a customer account
  they may offer a proper API instead.

## ABT Parcel Service

- ABT publishes no API. Its site has a form: `POST https://abtlogistics.com/track_submit.php`
  with `track_type=lr` and `mobile_lr=<LR number>`. The form also accepts the booking mobile
  number; the adapter never sends one.
- **In November 2026 ABT's online tracking was switched off** ("Temporarily Under
  Maintenance", phone 1800 425 2675). The adapter recognizes that and reports a temporary
  failure, so jobs are retried and then fail; they are not reported as "not found".
- The result page could not be examined. It is read by the general table reader (below).

## Balaji Roadlines

- Several unrelated companies use this name. The adapter is for **Sri Balaji Transport Lines
  (Regd.)**, mail domain sbrl.in, with a Tirupur branch and the only online LR tracking page
  found: `http://sribalajitransportlines.com/Website/tracking?lrno=<LR number>`.
  **If you use a different Balaji Roadlines, tell me which one; this adapter is not for it.**
- The site's HTTPS certificate had expired, so the address is plain HTTP.
- The result page could not be examined. It is read by the general table reader (below).

## What the three website adapters need from you

1. **Permission** from each company to query its site automatically. Then:
   `courier-engine admin set-scraping-authorized --provider <skyking|abt|balaji> --authorized true`
2. For `abt` and `balaji`: **one saved result page each** (open the tracking page in a
   browser, look up a real LR number, save the page as HTML). With it the reading can be
   confirmed or corrected and the page becomes a test fixture. For `skyking`: one real
   answer, to confirm the date format.

Until then `abt` and `balaji` use `adapters/tables.py`: it looks for an HTML table whose
header names a date column and a status column and reads its rows. A page that says "no
record found" is "not found". Any other page is reported as `SCHEMA_DRIFT` — the adapter
never guesses.

## Status wording

Delhivery has a fixed vocabulary. The other four describe statuses in sentences, which
cannot be mapped one by one. `adapters/india.py` reduces recognized wording to seven codes
(`booked`, `picked-up`, `in-transit`, `out-for-delivery`, `delivered`, `undelivered`,
`returned`), which migration 0013 maps. Wording it does not recognize is passed on
unchanged and appears in `make unmapped` for a person to map. The original sentence is
always kept as the event's description.

Times without an offset are taken as IST. Dates such as `03-04-2026` are read day first.

## Running the workers

```
make up-couriers                                   # all five
docker compose --profile delhivery up -d           # one
uv run python -m adapters.skyking                  # without Docker, from courier-adapters/
```

| Setting | Meaning |
|---|---|
| `DELHIVERY_WORKER_TOKEN`, `SAFEXPRESS_WORKER_TOKEN` | Worker tokens for fetching the credential from the Engine |
| `DELHIVERY_BASE_URL` | Default `https://track.delhivery.com` |
| `SAFEXPRESS_TOKEN_URL`, `SAFEXPRESS_TRACKING_URL`, `SAFEXPRESS_SCOPE` | Override the unconfirmed defaults |
| `SKYKING_BASE_URL`, `ABT_BASE_URL`, `BALAJI_BASE_URL` | Site addresses |

None of the five is polled automatically. Set a polling policy per provider once it works.

## Tests

`courier-adapters/tests/test_{delhivery,safexpress,skyking}_adapter.py`,
`test_abt_balaji_adapters.py`, `test_india_helpers.py`; `courier-engine/tests/indian_couriers.rs`.
Every test runs against a mocked courier. The example answers are built from documented or
observed field names; the ABT and Balaji result pages in the tests are invented.
