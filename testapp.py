from typing import List
from operator import itemgetter
from typing import Optional
from pathlib import Path
from langchain_openai import OpenAIEmbeddings
from langchain.prompts import PromptTemplate
from langchain_nvidia_ai_endpoints import NVIDIAEmbeddings
from langchain_community.vectorstores import Chroma
from langchain.indexes import SQLRecordManager, index
from langchain.text_splitter import MarkdownTextSplitter
from langchain.memory import ConversationBufferMemory, ConversationSummaryBufferMemory
from langchain.chains import ConversationalRetrievalChain, LLMChain
from langchain.chains.question_answering import load_qa_chain
from langchain.chains import RetrievalQA
from prompt import template
import chainlit as cl
import os
import tempfile
import getpass
import pymupdf4llm
from llms import get_graq_model

# NVIDIA API Key setup
if os.environ.get("NVIDIA_API_KEY", "").startswith("nvapi-"):
    print("Valid NVIDIA_API_KEY already in environment. Delete to reset")
else:
    nvapi_key = getpass.getpass("NVAPI Key (starts with nvapi-): ")
    assert nvapi_key.startswith("nvapi-"), f"{nvapi_key[:5]}... is not a valid key"
    os.environ["NVIDIA_API_KEY"] = nvapi_key

# Setting up the embeddings model
embeddings_model = NVIDIAEmbeddings(
    model="nvidia/nv-embed-v1", 
    api_key=os.getenv("NVIDIA_API_KEY"), 
    truncate="NONE", 
)

# Paths for the storage
current_working_directory = os.getcwd()
CODE_STORAGE_PATH = os.path.join(current_working_directory, 'Data2')
PERSIST_DIRECTORY = os.path.join(current_working_directory, 'chroma_db')

# Function to process PDF files and create the vector store
def process_python_files(pdf_storage_path: str):
    pdf_directory = Path(pdf_storage_path)
    docs = []
    splitter = MarkdownTextSplitter(chunk_size=430, chunk_overlap=0)

    for pdf_path in pdf_directory.glob("*.pdf"):
        md_text = pymupdf4llm.to_markdown(str(pdf_path))
        chunks = splitter.create_documents([md_text])
        for chunk in chunks:
            chunk.metadata['source'] = str(pdf_path)
        docs += chunks

    if os.path.exists(PERSIST_DIRECTORY):
        doc_search = Chroma(persist_directory=PERSIST_DIRECTORY, embedding_function=embeddings_model)
    else:
        doc_search = Chroma.from_documents(docs, embeddings_model, persist_directory=PERSIST_DIRECTORY)
        doc_search.persist()

    namespace = "chromadb/my_documents"
    record_manager = SQLRecordManager(
        namespace, db_url="sqlite:///record_manager_cache.sql"
    )
    record_manager.create_schema()

    index_result = index(
        docs,
        record_manager,
        doc_search,
        cleanup="incremental",
        source_id_key="source",
    )

    print(f"Indexing stats: {index_result}")
    return doc_search

# Initialize vector store and retrieval model
doc_search = process_python_files(CODE_STORAGE_PATH)
model = get_graq_model()
retriever = doc_search.as_retriever()

# Creating the prompt template
prompt = PromptTemplate(
    input_variables=["context", "history", "question"],
    template=template,
)

# Setting up the conversational chain with retrieval and memory
memory = ConversationSummaryBufferMemory(llm=model,
    memory_key="chat_history",
    input_key="question",
    output_key="answer",
    return_messages=True
)

conversational_chain = ConversationalRetrievalChain.from_llm(
    llm=model,  # Use your existing 'model' variable here
    retriever=retriever,
    memory=memory,
    combine_docs_chain_kwargs={"prompt": prompt},
    return_source_documents=True,
    return_generated_question=True,
    verbose=True
)

# Chainlit integration
def generate_sample_prompts():
    return [
        "Can you regenerate the parameter configuration file for the 'Disks' experiment, ensuring all parameters and their corresponding FITFLAG values are included, as well as sections [FCC Spheres], [Inputs], and [AI]?",
        "Can you add the parameter NewParameter with a value of 123 and its corresponding FITFLAG_NewParameter with a value of 00 to the 'Disks' experiment configuration file?",
        "Can you remove the parameter EditDebyeWaller and its corresponding FITFLAG_EditDebyeWaller from the 'Disks' experiment configuration file?",
        "Can you update the 'Disks' experiment configuration file by adding the parameter NewParameter with a value of 123 and its corresponding FITFLAG_NewParameter with a value of 00, and removing the parameter EditDebyeWaller and its corresponding FITFLAG_EditDebyeWaller?",
        
    ]
    
    
@cl.password_auth_callback
def auth_callback(username: str, password: str) -> Optional[cl.User]:
    """
    Authenticates a user based on their username and password.

    Args:
        username (str): The username of the user to authenticate.
        password (str): The password of the user to authenticate.

    Returns:
        Optional[cl.AppUser]: An instance of `cl.AppUser` if the user is authenticated, otherwise `None`.
    """
    if (username, password) == ("admin", "admin"):
        return cl.User(identifier="admin", metadata={"role": "ADMIN"})
    else:
        return None

@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("chain", conversational_chain)
    cl.user_session.set("chat_history", [])
    await cl.Message("Hello! I'm here to assist you with generating configuration files. What would you like to know or do? If you Write 'PROMPT' I will suggest you how to start chat with me.").send()

@cl.on_message
async def on_message(message: cl.Message):
    chain = cl.user_session.get("chain")
    chat_history = cl.user_session.get("chat_history")
    
    # Check for special commands
    if message.content.lower() == "clear history":
        chain.memory.clear()
        cl.user_session.set("chat_history", [])
        await cl.Message("Chat history cleared. How else can I assist you?").send()
        return
    
    # Check for PROMPT keyword
    if message.content.upper() == "PROMPT":
        sample_prompts = generate_sample_prompts()
        await cl.Message("Here are some sample prompts you can use:").send()
        for prompt in sample_prompts:
            await cl.Message(content=f"- {prompt}", author="Suggestion").send()
        return
    
    # Check if the message is a meta-question about the conversation
    if "last question" in message.content.lower():
        if chat_history:
            last_question = chat_history[-1][0]  # Get the last question from history
            await cl.Message(content=f"The last question you asked was: '{last_question}'").send()
        else:
            await cl.Message(content="There are no previous questions in the chat history.").send()
        return

    if "last answer" in message.content.lower():
        if chat_history:
            last_answer = chat_history[-1][1]  # Get the last answer from history
            await cl.Message(content=f"The last answer I gave was: '{last_answer}'").send()
        else:
            await cl.Message(content="There are no previous answers in the chat history.").send()
        return

    # Generate the response
    try:
        response = await chain.ainvoke(
            {
                "question": message.content,
                "history": chat_history  # Use 'chat_history' as the key
            }
        )
        
        answer = response['answer']
        
        # Update chat history
        chat_history.append((message.content, answer))
        cl.user_session.set("chat_history", chat_history)
        
        # Send the response back to the user
        await cl.Message(content=answer).send()
        
        # Extended list of keywords for configuration file requests
        config_keywords = [
            "can you regenerate the all parameters configuration file",
            "create configuration file",
            "write configuration setup",
            "generate file for my experiment",
            "make config file",
            "produce configuration",
            "setup file creation",
            "build config",
            "generate experiment parameters",
            "create setup file",
            "output configuration",
            "export settings file",
            "save experiment configuration",
            "compile setup parameters",
            "prepare config document",
            "construct experiment file",
            "formulate configuration"
        ]
        
        # Convert user's message to lowercase for case-insensitive comparison
        user_message_lower = message.content.lower()
        
        # Check if the user's message is requesting a configuration file
        if any(keyword in user_message_lower for keyword in config_keywords):
            # Parse and format the configuration
            config_lines = answer.split('\n')
            formatted_config = []
            current_section = None
            
            for line in config_lines:
                line = line.strip()
                if line.startswith('[') and line.endswith(']'):
                    current_section = line
                    formatted_config.append(line)
                elif '=' in line:
                    param, value = line.split('=', 1)
                    formatted_config.append(f"{param.strip()}={value.strip()}")
                elif line and current_section == '[AI]':
                    formatted_config.append(line)
            
            # Save the formatted configuration to a temporary text file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as temp_file:
                temp_file.write('\n'.join(formatted_config).encode('utf-8'))
                temp_file_path = temp_file.name
            
            # Provide download link to the user
            elements = [
                cl.File(
                    name="experiment.txt",
                    path=temp_file_path,
                    display="inline",
                ),
            ]

            await cl.Message(
                content="I've generated the configuration file based on our conversation. You can download it from here👇", 
                elements=elements
            ).send()
        
    except Exception as e:
        # Handle potential errors gracefully
        print(f"Error during chain invocation: {e}")
        await cl.Message(content="An error occurred while processing your request. Please try again.").send()