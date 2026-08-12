# Silent Heartbeat

An owner-only heartbeat that combines MemoryCompanion with PrivateCompanion.

On each interval, it:

1. Calls PrivateCompanion's public proactive preflight to obtain the current persona, relationship, expression, schedule, and delivery constraints.
2. Reads the configured private and group memory domains from MemoryCompanion.
3. Lets the configured model choose `silent` or one candidate message for the owner.
4. Sends the candidate through PrivateCompanion's public final review.
5. Sends only an approved message to the configured private session and records the delivery back to PrivateCompanion.

Any block, error, invalid model response, or rejected review releases the PrivateCompanion send lock and results in silence. Group sessions are read-only memory sources; this plugin never sends a heartbeat message to a group.

Every run logs a compact result at `INFO` and persists the last 50 redacted outcomes in the plugin data directory. Diagnostics include the stopping stage, preflight/review reason codes, memory-domain counts, model decision, duration, and error type, but never memory excerpts or message text.

```json
{
  "enabled": true,
  "private_session_id": "astrbot_onebot:FriendMessage:<owner_qq>",
  "authorized_group_session_ids": [
    "astrbot_onebot:GroupMessage:<group_qq>"
  ]
}
```

The only accepted model outputs are:

```json
{"action":"silent","target":"","message":""}
```

```json
{"action":"message","target":"private","message":"..."}
```
