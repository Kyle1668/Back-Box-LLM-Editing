from transformers import AutoConfig, AutoTokenizer, LlamaTokenizer, AutoModelForCausalLM, AutoModelForSeq2SeqLM, AutoModelForSequenceClassification, AutoModelForQuestionAnswering
import torch


def is_language_model(model_name):
    model_config = AutoConfig.from_pretrained(model_name, trust_remote_code=True)
    is_seq2seq_lm = model_config.architectures[0].endswith("ForConditionalGeneration")
    is_llm = model_config.architectures[0].endswith("ForCausalLM")
    return is_seq2seq_lm or is_llm


def get_model_objects(model_name, num_classes=None):
    model_config = AutoConfig.from_pretrained(model_name, trust_remote_code=True)
    is_seq2seq_lm = model_config.architectures[0].endswith("ForConditionalGeneration")
    is_qa_model = model_config.architectures[0].endswith("ForQuestionAnswering")
    is_llm = model_config.architectures[0].endswith("ForCausalLM")
    is_llama_based_model = is_llm and "llama" in model_name or "vicuna" in model_name
    # tokenizer = LlamaTokenizer.from_pretrained(model_name) if is_llama_based_model else AutoTokenizer.from_pretrained("bert-base-uncased")
    tokenizer = LlamaTokenizer.from_pretrained(model_name) if is_llama_based_model else AutoTokenizer.from_pretrained(model_name)
    model = None
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if is_llm:
        model = AutoModelForCausalLM.from_pretrained(model_name, trust_remote_code=True, torch_dtype=torch.float16, device_map="auto").eval()
    elif is_qa_model:
        model = AutoModelForQuestionAnswering.from_pretrained(model_name, trust_remote_code=True, torch_dtype=torch.float16).eval().to(device)
    elif is_seq2seq_lm:
        model = AutoModelForSeq2SeqLM.from_pretrained(model_name, trust_remote_code=True).eval().to(device)
    else:
        num_labels = num_classes if num_classes is not None else 2
        model = AutoModelForSequenceClassification.from_pretrained(model_name, trust_remote_code=True, num_labels=num_labels).eval().to(device)
    return tokenizer, model
