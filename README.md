# Silent Heartbeat

MemoryCompanion-aware heartbeat for one authorized private session and explicit group sessions. It reads every memory domain independently, defaults to `silent`, and only permits output to a configured target.

```json
{
  "enabled": true,
  "private_session_id": "astrbot_onebot:FriendMessage:<owner_qq>",
  "authorized_group_session_ids": [
    "astrbot_onebot:GroupMessage:<group_qq>"
  ]
}
```

The model must return exactly one JSON object:

```json
{"action":"silent","target":"","message":""}
```

Only a valid `private` or whitelisted `group:<group_id>` target can send a message. Invalid model output, unavailable MemoryCompanion, unavailable provider, or a target cooldown all result in silence.
