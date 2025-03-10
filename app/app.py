import os
import json
import base64
import asyncio
import discord
import chainlit as cl
from chainlit.discord.app import client as discord_client

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pinecone import Pinecone, ServerlessSpec  # type: ignore

from literalai import LiteralClient

load_dotenv()

# Discord limits the number of characters to 2000, which amounts to ~400 tokens.
DISCORD_MAX_TOKENS = 400

client = LiteralClient()
openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

pinecone_client = Pinecone(api_key=os.environ.get("PINECONE_API_KEY"))
pinecone_spec = ServerlessSpec(
    cloud=os.environ.get("PINECONE_CLOUD"),
    region=os.environ.get("PINECONE_REGION"),
)

cl.instrument_openai()

prompt_path = os.path.join(os.getcwd(), "app/prompts/rag.json")

# We load the RAG prompt in Literal to track prompt iteration and
# enable LLM replays from Literal AI.
with open(prompt_path, "r") as f:
    rag_prompt = json.load(f)

    prompt = client.api.get_or_create_prompt(
        name=rag_prompt["name"],
        template_messages=rag_prompt["template_messages"],
        settings=rag_prompt["settings"],
        tools=rag_prompt["tools"],
    )


def create_pinecone_index(name, client, spec):
    """
    Create a Pinecone index if it does not already exist.
    """
    if name not in client.list_indexes().names():
        client.create_index(name, dimension=1536, metric="cosine", spec=spec)
    return pinecone_client.Index(name)


pinecone_index = create_pinecone_index(
    "chainlit-rag-index", pinecone_client, pinecone_spec
)


@cl.set_starters
async def set_starters():
    return [
        cl.Starter(
            label="App Ideation",
            message="What kind of application can I create with Chainlit?",
            icon="/public/idea.svg",
        ),
        cl.Starter(
            label="How does authentication work?",
            message="Explain the different options for authenticating users in Chainlit.",
            icon="/public/learn.svg",
        ),
        cl.Starter(
            label="Chainlit hello world",
            message="Write a Chainlit hello world app.",
            icon="/public/terminal.svg",
        ),
        cl.Starter(
            label="Add a text element",
            message="How to add a text source chunk to a message?",
            icon="/public/write.svg",
        ),
    ]


@cl.step(name="Embed", type="embedding")
async def embed(question, model="text-embedding-3-small"):
    """
    Embed a question using the specified model and return the embedding.
    """
    embedding = await openai_client.embeddings.create(input=question, model=model)

    return embedding.data[0].embedding


@cl.step(name="Retrieve", type="retrieval")
async def retrieve(embedding, dataset_id, top_k):
    """
    Retrieve top_k closest vectors from the Pinecone index using the provided embedding.
    """
    if pinecone_index == None:
        raise Exception("Pinecone index not initialized")
    response = pinecone_index.query(
        vector=embedding,
        top_k=top_k,
        include_metadata=True,
        filter={"dataset_id": {"$eq": dataset_id}},
    )
    return response.to_dict()


async def get_relevant_chunks(question, dataset_id, top_k=5):
    """
    Retrieve relevant chunks from dataset based on the question embedding.
    """
    embedding = await embed(question)

    retrieved_chunks = await retrieve(embedding, dataset_id, top_k)

    return [match["metadata"]["text"] for match in retrieved_chunks["matches"]]


@cl.step(name="Documentation Retrieval", type="tool")
async def get_relevant_documentation_chunks(question, top_k=5):
    return await get_relevant_chunks(question, "dataset_documentation", top_k)


@cl.step(name="Cookbooks Retrieval", type="tool")
async def get_relevant_cookbooks_chunks(question, top_k=5):
    return await get_relevant_chunks(question, "dataset_cookbooks", top_k)


async def llm_tool(question, images_content):
    """
    Generate a response from the LLM based on the user's question.
    """
    messages = cl.user_session.get("messages", []) or []
    messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": question}, *images_content],
        }
    )

    settings = cl.user_session.get("settings", {}) or {}

    response = await openai_client.chat.completions.create(
        messages=messages,
        **settings,
        tools=cl.user_session.get("tools"),
        tool_choice="auto",
    )

    response_message = response.choices[0].message
    messages.append(response_message)
    return response_message


async def run_multiple(tool_calls):
    """
    Execute multiple tool calls asynchronously.
    """
    available_tools = {
        "get_relevant_documentation_chunks": get_relevant_documentation_chunks,
        "get_relevant_cookbooks_chunks": get_relevant_cookbooks_chunks,
    }

    async def run_single(tool_call):
        function_name = tool_call.function.name
        function_to_call = available_tools[function_name]
        function_args = json.loads(tool_call.function.arguments)

        function_response = await function_to_call(
            question=function_args.get("question"),
            top_k=function_args.get("top_k", 5),
        )
        return {
            "tool_call_id": tool_call.id,
            "role": "tool",
            "name": function_name,
            "content": "\n".join(function_response),
        }

    # Run tool calls in parallel.
    tool_results = await asyncio.gather(
        *(run_single(tool_call) for tool_call in tool_calls)
    )
    return tool_results


async def llm_answer(tool_results):
    """
    Generate an answer from the LLM based on the results of tool calls.
    """
    messages = cl.user_session.get("messages", []) or []
    messages.extend(tool_results)

    settings = cl.user_session.get("settings", {}) or {}

    stream = await openai_client.chat.completions.create(
        messages=messages,
        **settings,
        stream=True,
        stream_options={"include_usage": True},
    )

    answer_message = cl.Message(content="")

    token_count = 0
    async for part in stream:
        if part.usage:
            token_count = part.usage.completion_tokens
        elif token := part.choices[0].delta.content or "":
            await answer_message.stream_token(token)

    await answer_message.send()
    messages.append({"role": "assistant", "content": answer_message.content})

    if (
        cl.user_session.get("client_type") == "discord"
        and token_count >= DISCORD_MAX_TOKENS
    ):
        redirect_message = cl.Message(
            content="Looks like you hit Discord's limit of 2000 characters. Please visit https://help.chainlit.io to get longer answers."
        )
        await redirect_message.send()
        messages.append(redirect_message)

    return answer_message


@cl.step(name="RAG Agent", type="run")
async def rag_agent(question, images_content):
    """
    Coordinate the RAG agent flow to generate a response based on the user's question.
    """
    # Step 1 - Call LLM with tool: plan to use tool or give message.
    message = await llm_tool(question, images_content)

    # Potentially several calls to retrieve context.
    if not message.tool_calls:
        answer_message = cl.Message(message.content)
        await answer_message.send()
        return message.content

    # Step 2 - Run the tool calls.
    tool_results = await run_multiple(message.tool_calls)

    # Step 3 - Call LLM to answer based on contexts (streamed).
    return (await llm_answer(tool_results)).content


@cl.on_chat_start
async def on_chat_start():
    """
    Send a welcome message and set up the initial user session on chat start.
    """

    client_type = cl.user_session.get("client_type")

    cl.user_session.set("messages", prompt.format_messages())
    cl.user_session.set("settings", prompt.settings)
    cl.user_session.set("tools", prompt.tools)

    if client_type == "discord":
        prompt.settings["max_tokens"] = DISCORD_MAX_TOKENS


async def use_discord_history(limit=10):
    messages = cl.user_session.get("messages", [])
    channel: discord.abc.MessageableChannel = cl.user_session.get("discord_channel")

    if channel:
        cl.user_session.get("messages")
        discord_messages = [message async for message in channel.history(limit=limit)]

        # Go through last `limit` messages and remove the current message.
        for x in discord_messages[::-1][:-1]:
            messages.append(
                {
                    "role": (
                        "assistant"
                        if x.author.name == discord_client.user.name
                        else "user"
                    ),
                    "content": (
                        x.clean_content
                        if x.clean_content is not None
                        else x.channel.name
                    ),  # first message is empty
                }
            )


# Function to encode an image
def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


@cl.on_message
async def main(message: cl.Message):
    """
    Main message handler for incoming user messages.
    """
    images_content = []
    if message.elements:
        images = [file for file in message.elements if "image" in file.mime]

        # Only process the first 3 images
        images = images[:3]

        images_content = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.mime};base64,{encode_image(image.path)}"
                },
            }
            for image in images
        ]
    # The user session resets on every Discord message. Add previous chat messages manually.
    await use_discord_history()

    await rag_agent(message.content, images_content)
