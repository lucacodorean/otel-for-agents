# Invalid Request

The following output format should be used in order to provide a structured invalid response:

```json
{
    "question": <string>,
    "error_code": <int>,
    "metadata": {
        "error": <string>
    }
}
```

Each field represents as follows:

| Field | Meaning | 
|-------|---------|
| question | The actual question sent as parameter |
| error_code | A numeric HTTP_CODE specific to both server or client errors {4xx or 5xx} |
| metadata.error | The encountered issue |

Any other text should be discharged.