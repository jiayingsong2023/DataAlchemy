import asyncio
import websockets
import json

async def test_ws():
    uri = "ws://localhost:8000/ws/chat"
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected to WebSocket!")
            await websocket.send(json.dumps({"query": "test"}))
            while True:
                response = await websocket.recv()
                print(f"Received: {response}")
                data = json.loads(response)
                if data.get("type") == "answer":
                    break
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_ws())
