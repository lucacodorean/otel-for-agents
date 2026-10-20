# Formatted output

The following output format should be used in order to provide a structured valid response:

```json
{
    "question": <string>,
    "response": <string>,
    "metadata": {
        "reference_interval": { 
            "start": <DateTime>, 
            "end": <DateTime> 
        },
        "promql": <string>,
        "points": <array>
    }
}
```

Each field represents as follows:

| Field | Meaning | 
|-------|---------|
| question | The actual question sent as parameter |
| response | The response in natural language |
| metadata.reference_interval | Reference timeframe for the query |
| metadata.promql | The PromQL resulted based on the given question |
| metadata.points | The array of points on the graph that are the result of the PromQL query |