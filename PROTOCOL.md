# Terma Smart — Protocol reference

Reverse-engineered from the official Android app **`com.termaconnect.mobile`
v1.32** (`VERSION_CODE = 89`, build `release`), cross-referenced against a
packet capture of LAN traffic. All field names, types, action IDs, and
encodings below are taken directly from the Kotlin sources (decompiled with
jadx). Cloud-side endpoint paths come from the same sources; request/response
payload schemas are described by the `*RequestPayload` / `*ResponsePayload`
data classes.

## 1. Architecture overview

The phone app talks over three independent channels:

| Channel | Endpoint | Transport | Purpose |
|---|---|---|---|
| **LAN control** | thermostat IP, **TCP 5005** | plaintext JSON | direct control when on the same Wi-Fi as the device |
| **LAN discovery** | broadcast, **UDP 2349** | plaintext JSON | finding thermostats during onboarding |
| **User cloud API** | `https://api.termasmart.com` | HTTPS + JSON (Bearer auth) | login, account, house/zone topology, schedules, notifications |
| **Device cloud** | `api-devices.termasmart.com` | TLS (device-initiated) | the thermostat's own outbound connection to Terma's cloud — used as a remote-control fallback when the phone is off-LAN |

Constants from `com.termaconnect.mobile.BuildConfig`:

```
APP_API_SERVER                                = "https://api.termasmart.com"
DEVICE_API_SERVER                             = "api-devices.termasmart.com"
APP_DEVICE_CONECTION_TCP_PORT                 = 5005
APP_DEVICE_BROADCAST_PORT                     = 2349
APP_DEVICE_BROADCAST_DISCOVERY_SECONDS_TIMEOUT= 10
APP_CORE_HTTP_TIMEOUT_SECONDS                 = 10
APP_DEVICE_REGISTRATION_TIMEOUT_SECONDS       = 60
APP_DEVICE_EXPIRATION_TIME                    = 3600   # seconds
```

The HTTP client is **Fuel** (`com.github.kittinunf.fuel`). Base path is
`APP_API_SERVER` (`HttpClientExtensionKt.setEndpoint`). Default headers
(`HttpClientExtensionKt.addGlobalHeaders`):

```
Content-Type:    application/json; charset=utf-8
Accept-Encoding: gzip
Connection:      close
```

Outgoing requests get `Authorization: Bearer <token>` added by
`RequestInterceptor`, where `<token>` is read from the local storage provider.
The cloud responds with `401 Unauthorized` when the access token is stale; the
`ResponseInterceptor` then calls `tryRefreshToken` and replays the request
once.

---

## 2. LAN control — TCP 5005

Implemented in `core.api.internal.requests.device.local.*` and dispatched by
`core.network.HouseNetworkService` / `HouseRequestRouter`.

### 2.1 Transport

- The app opens a fresh TCP connection to **`<device-ip>:5005`** per request.
- Sends one JSON object (UTF-8, no framing, no trailing newline).
- Reads until one complete JSON object has been received (brace counting), then
  closes.
- Default timeout: 10 s (from `APP_CORE_HTTP_TIMEOUT_SECONDS`).
- **No authentication, no encryption** on this channel. Any host on the LAN
  that knows the device's serial can drive it.

### 2.2 Common request envelope

Every request implements `ILocalRequest` and serialises (via
`kotlinx.serialization`) to:

```json
{
  "serial":    "<deviceIdentifier>",   // e.g. "HBKB693DAP#1#D1"
  "timestamp": 1779779771,              // unix epoch seconds, set by app via DateTime.epochUnixNow()
  "actionId":  <int>,                   // see action ID table
  ...                                    // request-specific fields
}
```

Field ordering on the wire is the declaration order in the Kotlin data class,
but the device parses by name — order is **not** significant.

### 2.3 Action IDs (`LocalRequestType`)

```kotlin
enum class LocalRequestType(val value: Int) {
    ConfigureNetwork(7),
    UpdateSchedule(8),
    MeasurementsTemperatureSensor(0),
    MeasurementsWindowSensor(1),
    MeasurementsHeater(2),
    MeasurementsHeaterBuiltIn(2),
    MeasurementsHeatThermostaticHeader(3),
    ConfigureTemperatureSensor(4),
    ConfigureWindowSensor(4),
    ConfigureHeater(5),
    ConfigureHeaterBuiltIn(5),
    ConfigureHeatThermostaticHeader(6),
    ConfigureApi(14),
    ExtendedControlApi(26),
}
```

Plus `DeviceTelemetryFetchCommand.fetchActionId = 15` (a hardcoded variant of
"get telemetry" that requires a MAC address). The action ID actually used for
a given device depends on `DeviceTypes`:

| Device type | telemetry id | configure id |
|---|---|---|
| `Heater`, `HeaterBuiltIn` | 2 | 5 |
| `TemperatureSensor`, `WindowSensor` | 0 / 1 | 4 |
| `HeatThermostaticHeader` | 3 | 6 |

(`HouseDeviceExtensionsKt.toActionId(deviceType, measurement: Boolean)`.)

### 2.4 Common response envelope (`LocalResponse`)

```json
{
  "errorCode": "None" | "InvalidDevice",
  "telemetry": { ... DeviceTelemetry ... } | null,
  "request":   { ... echo of original request ... } | null
}
```

Only two error codes are defined (`LocalResponseCode`): `None` (= 0) and
`InvalidDevice` (= 1).

### 2.5 Telemetry response — `DeviceTelemetry`

All fields except `serial` are optional. Types are the Kotlin signatures.

| JSON field | Type | Units / notes |
|---|---|---|
| `serial` (`serialNumber` internally) | String | required |
| `fwVersion` | String | e.g. `"2.0.HBDF14F41"` |
| `timestamp` | Long | unix epoch seconds, **device clock** |
| `batteryLevel` | Int | percent 0–100 |
| `errorsFlag` | String | 32-hex-char error bitmap, e.g. `"00000000…"` |
| `temperature` | Double | room temp, **Kelvin** |
| `humidity` | Int | percent (0 when sensor absent) |
| `heaterTemperature` | Double | heating element temp, **Kelvin** |
| `state` | Int | device state (only present on newer fw) |
| `isEnabled` | Int 0/1 | mirrors `enable` |
| `isDryerOn` | Long | typed Long, not Int — likely countdown; 0 when off |
| `isCalibrateOn` | Int 0/1 | sensor calibration active |
| `isParentalControlOn` | Int 0/1 | |
| `isHeating` | Int 0/1 | element actively energised |
| `heatingCoefficient` | Double 0..1 | mirrors `zoneWeight` |
| `manualMode` | Int | `0`/`1`/`2` = Manual / ManualWithTimer / ManualScheduleAware. Telemetry also reports `3` ≈ "no manual override / following schedule" |
| `powerUsage` | Double | kWh, cumulative energy |
| `schedule` | String | quoted bracketed list — see §2.7 |

Some response/configuration fields the device returns but the app's
`DeviceTelemetry` ignores (only visible if you read raw JSON):

- `secureHttp`, `defaultSecureHttp` — cloud-channel flags
- `hwVersion`, `devClass`, `wfxFwVersion` — extra hardware identifiers
- `isParentalControlOn` shape is sometimes `Int`, sometimes `Long`

The configured `powerCapabilities` (Heating Power, in watts) is **settable
but is not in the telemetry response model** — it's only persisted on the
device, not echoed back here.

### 2.6 Encodings

- **Temperatures**: Kelvin × 100 (so `21.0 °C` → `294.15 K` → `29415`).
  Used for `setTemperature` and for setpoints inside `schedule`.
- **`schedule` (in telemetry)** is a JSON **string** containing a bracketed
  list:

```
"[ <weekSeconds>, <K×100>, <weekSeconds>, <K×100>, … ]"
```

  where `weekSeconds` is seconds since the start of the week. The week begins
  at Monday 00:00 (`ScheduleConverter.weekBeginSeconds = 0`,
  `daySeconds = 86400`, `weekSeconds = 604800`).
- **`schedule` (in `UpdateSchedule` request, actionId 8)** is a JSON **array of
  Long** with the same `(sec, K*100, sec, K*100, …)` interleaving — not a
  string.

### 2.7 Request schemas

Each row below documents one Kotlin request class — its actionId, the extra
JSON fields it contributes beyond the common envelope, and what they mean.

#### `DeviceTelemetryCommand` — actionId 2 (or per device type)

```json
{ "serial": "...", "timestamp": ..., "actionId": 2 }
```

Read full telemetry.

#### `DeviceTelemetryFetchCommand` — actionId **15**

```json
{ "serial": "...", "timestamp": ..., "actionId": 15, "mac": "AA:BB:CC:DD:EE:FF" }
```

Same telemetry, but requires the device's MAC address. Used by paths that
already know it.

#### `StandbyRequest` — actionId 5

```json
{ "actionId": 5, "serial": "...", "timestamp": ..., "enable": 0 | 1 }
```

Power the heater on / off. Mirrored back as `isEnabled`.

#### `DryerRequest` — actionId 5

```json
{ "actionId": 5, "serial": "...", "timestamp": ...,
  "dryer": 0 | 1, "dryerTimer": 3600 }
```

Towel-rail boost. `dryerTimer` is duration in seconds (the app sends 3600
by default).

#### `ParentalControlRequest` — actionId 5

```json
{ "actionId": 5, "serial": "...", "timestamp": ...,
  "parentalControl": 0 | 1 }
```

Child lock.

#### `ChangeHeatingCoefficientRequest` — actionId 5

```json
{ "actionId": 5, "serial": "...", "timestamp": ...,
  "zoneWeight": 0.8 }
```

Heating level slider, fraction `0.0..1.0` (this is the app's 0–100 %). Comes
back as `heatingCoefficient`.

#### `PowerCapabilitiesCommand` — actionId 5

```json
{ "actionId": 5, "serial": "...", "timestamp": ...,
  "powerCapabilities": 200 }
```

Rated heating-element power in watts ("Heating Power" in the UI). Persisted
on the device, **not** included in subsequent telemetry responses.

#### `ChangeTemperatureRequest` — actionId 5

```json
{ "actionId": 5, "serial": "...", "timestamp": ...,
  "manualMode": 0 | 1 | 2,
  "manualTimer": null | 3600,
  "setTemperature": 29415 }
```

Manual temperature override. `setTemperature` is **Kelvin × 100**.
`manualMode` (`DeviceTemperatureChangeMode`):

| Value | Name | Meaning |
|---|---|---|
| 0 | `Manual` | indefinite override (`manualTimer = null`) |
| 1 | `ManualWithTimer` | override for `manualTimer` seconds, then revert |
| 2 | `ManualScheduleAware` | override only until the next schedule entry |

#### `ExitManualModeRequest` — actionId 5

```json
{ "actionId": 5, "serial": "...", "timestamp": ...,
  "exitManualMode": 1 }
```

Drop any manual override and resume the schedule immediately.

#### `CalibrateRequest` — actionId 5

```json
{ "actionId": 5, "serial": "...", "timestamp": ...,
  "calibrate": 0 | 1 }
```

Enter / exit sensor calibration mode. Mirrored as `isCalibrateOn`.

#### `ChangeZoneTemperatureSensor` — actionId 5

```json
{ "actionId": 5, "serial": "...", "timestamp": ...,
  "zoneSensor": "AA:BB:CC:DD:EE:FF" }
```

Bind a remote temperature sensor to this heater's zone (MAC address).

#### `DeviceGroupCommand` — actionId 5

```json
{ "actionId": 5, "serial": "...", "timestamp": ...,
  "groupedDevicesMac": ["AA:BB:CC:DD:EE:FF", ...] }
```

Group sibling heaters that should heat together.

#### `DeviceInZoneCommand` — actionId 5

```json
{ "actionId": 5, "serial": "...", "timestamp": ...,
  "zoneDevicesMac": [["AA:..."], ["BB:..."]] }
```

Two-level array describing zone ↔ device topology.

#### `ScheduleUpdateRequest` — actionId **8** (`UpdateSchedule`)

```json
{ "actionId": 8, "serial": "...", "timestamp": ...,
  "schedule": [10800, 29415, 18000, 29115, ...],
  "exitManualMode": 0 | 1 }
```

Replace the weekly schedule. Sent as a flat array of longs — alternating
`(weekSeconds, K*100)`. Setting `exitManualMode: 1` also clears any active
override.

#### `ConfigureNetworkRequest` — actionId **7** (`ConfigureNetwork`)

```json
{ "actionId": 7, "serial": "...", "timestamp": ...,
  "ssid": "...", "password": "..." }
```

Reconfigure the device's Wi-Fi. Sent during pairing while the phone is
connected to the device's hotspot.

#### `SetApiPortLocalRequestRequest` — actionId **14** (`ConfigureApi`)

```json
{ "actionId": 14, "serial": "...", "timestamp": ...,
  "apiUrl": "api-devices.termasmart.com",
  "apiPort": 443 }
```

Repoint the device's cloud endpoint. **Dangerous** — wrong values can detach
the device from the cloud permanently.

#### `SetSecureCommunicationLocalRequest` — actionId **26** (`ExtendedControlApi`)

```json
{ "actionId": 26, "serial": "...", "timestamp": ...,
  "setSecuredHttp": 0 | 1 }
```

Toggle the cloud channel between plain HTTP and TLS (visible in telemetry as
`secureHttp`).

---

## 3. LAN discovery — UDP 2349

Implemented in `core.network.HouseNetworkService` and used by
`core.services.network.DeviceRegistrationService`.

### 3.1 Transport

- The phone broadcasts to its current IPv4 broadcast address, port **2349**.
- Datagram body is one JSON object, UTF-8, padded to up to 1024 bytes by
  whitespace in the receive buffer (the receiver trims).
- Responses come back as unicast datagrams to the same socket.
- Default discovery timeout: 10 s (`APP_DEVICE_BROADCAST_DISCOVERY_SECONDS_TIMEOUT`).

### 3.2 Frame envelope (`DeviceConfigurationRequest`)

All frame fields are namespaced with the prefix `_terma_message_`. The
`@SerialName` annotations in the source pin the exact JSON keys.

Request, sent by the phone:

```json
{
  "_terma_message_identifier": "<random-uuid>",
  "_terma_message_type":       "DeviceConfigurationRequest",
  "_terma_message_sender":     "192.168.1.42",
  "_terma_message_recipients": ["HBKB693DAP#1#D1", "..."] | null,
  "_terma_message_channel":    0,
  "_terma_message_payload":    {
    "parameters": [
      { "name": "...", "value": "..." }, ...
    ]
  }
}
```

The `type` string is the simple class name —
`DeviceConfigurationRequest.Companion.create()` literally sets
`type = Reflection.getOrCreateKotlinClass(DeviceConfigurationRequest::class).simpleName`.
`recipients = null` means "any device"; a non-null list filters by serial.

Response, sent by each thermostat (`DeviceConfigurationResponse`):

```json
{
  "_terma_message_identifier":        "<echoed uuid>",
  "_terma_message_type":              "DeviceConfigurationResponse",
  "_terma_message_sender":            "<device ip>",
  "_terma_message_payload":           { "parameters": [...] },
  "_terma_message_device_identifier": "HBKB693DAP#1#D1"
}
```

The phone uses the response's `_terma_message_sender` (device IP) to start the
TCP 5005 control session, and `_terma_message_device_identifier` as the
`serial` for all subsequent requests.

---

## 4. Cloud — `https://api.termasmart.com`

Uses Fuel + kotlinx.serialization. All bodies are wrapped in a `BaseRequest`
envelope:

```json
{
  "identifier": "<uuid-v4>",   // per-request, generated client-side
  "payload":    { ... }         // request-specific, polymorphic
}
```

Responses use a parallel envelope (`IBaseResponse`) with `payload` being one of
the typed response classes plus an error model.

### 4.1 Authentication

`Authorization: Bearer <accessToken>` on every request after sign-in. The
token is stored locally and injected by `RequestInterceptor`. On 401 the
`ResponseInterceptor` invokes `tryRefreshToken` and replays the original
request once.

### 4.2 Endpoint inventory

These are the path suffixes that appear in the client services. The base URL
is `APP_API_SERVER` from `BuildConfig`.

| Path | Verb(s) | Service | Purpose |
|---|---|---|---|
| `auth/signIn` | POST | `TermaUserAuthService` | email/password login (body: `LoginUserRequestPayload`) |
| `auth/refresh` | POST | `TermaUserAuthService` | refresh access + refresh tokens |
| `auth/external/signIn` | POST | `TermaUserAuthService` | external (Facebook/Apple/Google) login |
| `auth/register` | POST | `TermaUserAuthService` | create a local account |
| `auth/external/register` | POST | `TermaUserAuthService` | register via external provider |
| `auth/confirmEmail` | POST | `TermaUserAuthService` | finish email verification |
| `auth/resetPassword` | POST | `TermaUserAuthService` | start password reset |
| `auth/confirmChangePassword` | POST | `TermaUserAuthService` | finish password reset / confirm change |
| `auth/changePassword` | POST | `TermaUserAuthService` | change current password |
| `house` (`getAllHouses`, `createHouse`, `deleteHouse`) | POST | `TermaConnectHouseService` | house collection ops |
| `house/topology` | POST | `TermaConnectHouseService` | full house structure (zones, devices, schedules) |
| `house-device/status/telemetry/batch` | POST | `TermaConnectHouseStatusService` | live telemetry for every device in a house (`HouseTelemetryRequestPayload{houseIdentifier}` → `HouseTelemetryResponsePayload{telemetry: [{serialNumber, telemetry: <DeviceTelemetry §2.5>}, …]}`) |
| `house-device/status/telemetry/aggregate` | POST | `TermaConnectHouseStatusService` | per-zone historical telemetry aggregates over a time range (`ZoneTelemetryAggregateRequestPayload{houseIdentifier, zoneName, filter: {from, to}}`) |
| `house/notifications` | POST | `TermaConnectHouseNotificationService` | per-house notifications |
| `house/notifications/device/register` | POST | `TermaConnectHouseNotificationService` | register an FCM device token |
| `house-location` (`performForwardGeoCoding`, `performReverseGeoCoding`, `getCurrentWeather`) | POST | `TermaHouseLocationService` | geocoding + weather lookup |
| `user-location` (`updateUserLocation`, `allowLocationTracking`) | POST | `TermaUserLocationService` | smart-home presence updates |
| `users-settings` | GET / PUT | `TermaUserSettingsService` | per-user preferences blob |
| `users-settings/user-delete` | POST | `TermaUserSettingsService` | account deletion |
| `addHouseDevice`, `addHouseUser`, `createHouseZone`, `createHouseSchedule`, `addHouseZoneSchedule`, `updateHouseInfo`, `updateHouseZone`, `updateHouseConfiguration`, `updateHouseSchedule`, `deleteHouseDevice`, `deleteHouseUser`, `deleteHouseZone`, `deleteHouseSchedule`, `deleteHouseZoneSchedule` | POST | `TermaConnectHouseService` | individual mutation methods on the house model |

Method names are also used verbatim as path suffixes for the simpler RPC-style
endpoints (e.g. `addHouseDevice` → `POST /addHouseDevice`).

### 4.3 Auth request payloads

`POST auth/signIn` — `BaseRequest<LoginUserRequestPayload>` (the Kotlin class
is named ``LoginUserRequestPayload`` but the actual path is ``auth/signIn`` —
verified from ``r3 = "auth/signIn"`` in ``TermaUserAuthService.java``):

```json
{
  "identifier": "<uuid>",
  "payload": { "email": "...", "password": "..." }
}
```

Response payload contains an access token, refresh token, and basic profile
fields (typed as `LoginUserResponsePayload`).

`POST auth/refresh` — `BaseRequest<RefreshTokenRequestPayload>`:

```json
{
  "identifier": "<uuid>",
  "payload": { "refreshToken": "..." }
}
```

Other auth endpoints follow the same pattern with their dedicated
`*RequestPayload` body.

### 4.4 Models exchanged

The cloud uses the same top-level models as the LAN side wherever they
overlap:

- `HouseModel`, `HouseZoneModel`, `HouseDeviceModel`, `HouseUserModel`,
  `HouseConfigurationModel`, `WorkingMode`.
- `Schedule`, `ScheduleEntry`, `ScheduleDay`, `ScheduleDayOfWeek` (Monday=1,
  Sunday=7).
- `DeviceTelemetry` (same shape as §2.5 — the cloud effectively proxies the
  device's last-known telemetry).
- `DeviceTypes`, `DeviceTemperatureChangeMode`, `TemperatureConfiguration`.

Notifications use `core.api.models.house.notifications.*` with a payload that
references device + serial + severity.

### 4.5 Error envelopes

`core.api.internal.base.response.errors` defines:

- `AuthenticationFailureResponse` / `…ResponsePayload` — wraps 401 errors with
  reason codes.
- `ModelValidationErrorResponse` / `…ResponsePayload` — 400 with a list of
  per-field validation issues.
- `UnexpectedExceptionResponse` / `…ResponsePayload` — server-side 5xx with a
  message string.

The interceptor first inspects `BaseResponseType` to decide which of these to
deserialise.

### 4.6 House-status hybrid path

`TermaConnectHouseStatusService` exposes a "pull" operation that takes
**either** the local route (if the phone is on-LAN with the device and
`secureHttp` allows it) **or** the cloud route. The decision is per-device,
based on whether `refreshEndpoints` succeeded for that device. From the app's
point of view a single API call may dispatch to:

- the LAN protocol from §2 (one `DeviceTelemetryCommand`), or
- a cloud `POST /house-device/status/telemetry/batch` (per-house batch),
  whose response carries a `DeviceTelemetry` JSON object (same shape as
  §2.5) for each device in the house.

---

## 5. Device-cloud channel — `api-devices.termasmart.com`

This channel is initiated **by the thermostat**, not the phone, so the app
source documents only its endpoint — not its protocol. From a packet capture
it appears as a long-lived outbound TLS connection from the device to TCP 443
of `api-devices.termasmart.com` (observed IP in the trace was a Google-hosted
load balancer, suggesting it's fronted on GCP).

The two LAN actions that interact with this channel are:

- `ConfigureApi` (actionId 14) — set `apiUrl` / `apiPort` on the device.
- `ExtendedControlApi` (actionId 26) with `setSecuredHttp` — toggle whether
  the device uses HTTPS or plain HTTP for this channel.

The app's telemetry uses `secureHttp` / `defaultSecureHttp` to surface the
current state.

---

## 6. Quick reference (Python)

The reference Python implementation in this repo (`terma.py` + `terma_cli.py`)
covers all LAN actions listed in §2 with friendly names and helpers for
Kelvin↔Celsius and schedule encoding. The cloud API (§4) is implemented by
`TermaCloudClient` in the same module — `BaseRequest` envelopes, bearer-token
auth with automatic single-shot refresh on 401, and named methods for the
auth, house, notification, geocoding, user-location, and user-settings
endpoints; the device-cloud channel (§5) is device-initiated and therefore
out of scope.

## 7. Source map

If you need to verify any specific field, the decompiled sources are in
`jadx-out/sources/com/termaconnect/mobile/`. The most useful entry points:

```
core/api/internal/requests/device/local/    # all LAN request classes
core/api/internal/requests/device/local/LocalRequestType.java   # action ID enum
core/api/internal/requests/device/local/LocalResponse.java      # response envelope
core/api/models/telemetry/DeviceTelemetry.java                  # telemetry shape
core/api/models/device/DeviceTemperatureChangeMode.java         # manualMode enum
core/network/HouseNetworkService.java                           # UDP 2349 transport
core/network/frames/messages/DeviceConfigurationRequest.java    # discovery frame
core/api/client/                                                # cloud HTTP services
core/providers/http/HttpClient.java                             # Fuel wrapper
core/providers/http/interceptors/RequestInterceptor.java        # Bearer-token injection
BuildConfig.java                                                # all endpoints + constants
```
