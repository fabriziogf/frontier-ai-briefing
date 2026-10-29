# Feedback worker

Small Cloudflare Worker that receives signed feedback links (spec section 8.1):

```
https://<site>/fb?t=<type>&id=<id>&s=<signal>&sig=<hmac>
```

It validates the HMAC and appends the event to a queue. The next pipeline run syncs events into SQLite. Built in M4.
