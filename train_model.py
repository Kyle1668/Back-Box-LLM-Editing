from typing import Callable, Dict, List, Optional, Tuple, Union
from torch import nn
from transformers import DataCollatorWithPadding, DataCollatorForSeq2Seq, Trainer, TrainingArguments, Seq2SeqTrainer, Seq2SeqTrainingArguments, AutoModel, LlamaTokenizer, AutoTokenizer, pipeline
from peft import get_peft_config, get_peft_model, prepare_model_for_int8_training, get_peft_model_state_dict, LoraConfig, TaskType
from sklearn.metrics import classification_report
from datasets import Dataset, DatasetDict
from argparse import ArgumentParser
from time import time
from tqdm import tqdm
import pandas as pd
import numpy as np
import evaluate
import json
import os
import torch
from transformers.data.data_collator import DataCollator
from transformers.modeling_utils import PreTrainedModel
from transformers.tokenization_utils_base import PreTrainedTokenizerBase
from transformers.trainer_callback import TrainerCallback
from transformers.trainer_utils import EvalPrediction
from transformers.training_args import TrainingArguments
import wandb

from util_modeling import get_model_objects, is_language_model, is_large_language_model
from util_icl import generate_classification_prompt, get_static_exemplars, get_dynamic_exemplars
from util_data import get_formatted_dataset
from adaptive_methods import GenericDataset

# Set global tokenizer for computing metrics
GLOBAL_TOKENIZER = None



class RewriteTrainer(Trainer):
    tokenizer = None
    sentence_encoder_tokenizer = AutoTokenizer.from_pretrained("princeton-nlp/sup-simcse-roberta-large")
    sentence_encoder_model = AutoModel.from_pretrained("princeton-nlp/sup-simcse-roberta-large").to("cuda").eval()
    task_tokenizer = get_model_objects("Kyle1668/boss-sentiment-bert-base-uncased", 3)[0]
    task_model = get_model_objects("Kyle1668/boss-sentiment-bert-base-uncased", 3)[1].to("cuda").eval()
    id_centroid = torch.load("notebooks/dynasent_analysis/amazon_train_centroid_humarin-chatgpt_paraphraser_on_T5_base.pt").to("cuda")
    # id_centroid = torch.load("notebooks/dynasent_analysis/amazon_validation_centroid_stabilityai-StableBeluga-7B.pt").to("cuda")


    def parse_class_label(self, inputs):
        labels_tokens = inputs.pop("labels")
        is_leading_padding = (labels_tokens[:, :3] == labels_tokens[0][0]).all().item()
        token_ids = labels_tokens[:, -3:] if is_leading_padding else labels_tokens[:, :3]
        return torch.tensor(np.array(self.tokenizer.batch_decode(token_ids, skip_special_tokens=True), dtype=int))

    def get_batch_embeddings(self, inputs, model):
        if is_large_language_model(model.config.name_or_path):
            return model(**inputs, output_hidden_states=True)["hidden_states"][-1].mean(dim=1)
        else:
            return model(**inputs, decoder_input_ids=inputs["input_ids"], output_hidden_states=True)["encoder_last_hidden_state"].mean(dim=1)

    def compute_loss(self, model, inputs, return_outputs=False):
        labels = self.parse_class_label(inputs)
        batch_embeddings = self.get_batch_embeddings(inputs, model)
        mean_centroid_similarity = torch.cosine_similarity(batch_embeddings, self.id_centroid).mean()
        mean_centroid_distance = torch.dist(batch_embeddings, self.id_centroid).mean()

        with torch.no_grad():
            sequence_logits = model.generate(**inputs, do_sample=False, max_new_tokens=100)
            generations = [text.split("### Assistant:")[1].strip() if "### Assistant:" in text else text for text in self.tokenizer.batch_decode(sequence_logits, skip_special_tokens=True)]
            correct_class_probs = []
            for probs, label in zip(self.get_class_probs(generations), labels):
                correct_class_probs.append(probs[label])
            mean_class_prob = torch.tensor(correct_class_probs).mean()

        label_preservation_term = 10 * (1 - mean_class_prob)
        id_distance_term = torch.log(mean_centroid_distance) / 5
        batch_loss = label_preservation_term + id_distance_term

        print(f"Mean Class Prob: {mean_class_prob} | ID Centroid Distance: {mean_centroid_distance} | Batch Loss: {batch_loss}")
        return (batch_loss, generations) if return_outputs else batch_loss


    # def compute_loss(self, model, inputs, return_outputs=False):
    #     labels_tokens = inputs.pop("labels")
    #     labels = torch.tensor(np.array(self.tokenizer.batch_decode(labels_tokens[:, :3], skip_special_tokens=True), dtype=int))
    #     generations = model.generate(**inputs, do_sample=False, max_new_tokens=100)
    #     output_texts = self.tokenizer.batch_decode(generations, skip_special_tokens=True)
    #     output_texts = [text.split("### Assistant:")[1].strip() if "### Assistant:" in text else text for text in output_texts]
    #     output_probs = self.get_class_probs(output_texts)
    #     input_texts = self.tokenizer.batch_decode(inputs.input_ids, skip_special_tokens=True)
    #     for i in range(len(input_texts)):
    #         input_texts[i] = input_texts[i].split("User: ")[1].split("### Assistant")[0].strip() if "User: " in input_texts[i] else input_texts[i]

    #     original_embeddings = self.get_embeddings(input_texts)
    #     rewrite_embeddings = self.get_embeddings(output_texts)

    #     losses = []
    #     running_loss = None
    #     for original_embedding, rewrite_embedding, probs, label in zip(original_embeddings, rewrite_embeddings, output_probs, labels):
    #         class_prob = probs[label]
    #         id_centroid_sim = torch.cosine_similarity(rewrite_embedding.unsqueeze(0), self.id_centroid.unsqueeze(0))
    #         rewrite_original_sim = torch.cosine_similarity(rewrite_embedding.unsqueeze(0), original_embedding.unsqueeze(0))

    #         # Distances
    #         id_centroid_dist = torch.dist(rewrite_embedding.unsqueeze(0), self.id_centroid.unsqueeze(0))
    #         rewrite_original_dist = torch.dist(rewrite_embedding.unsqueeze(0), original_embedding.unsqueeze(0))
    #         id_centroid_chord_distance = 0.5 * np.linalg.norm((rewrite_embedding.detach().cpu().numpy() - np.mean(rewrite_embedding.detach().cpu().numpy())) - (self.id_centroid.detach().cpu().numpy() - np.mean(self.id_centroid.detach().cpu().numpy()))) ** 2 / (np.linalg.norm(rewrite_embedding.detach().cpu().numpy() - np.mean(rewrite_embedding.detach().cpu().numpy())) ** 2 + np.linalg.norm(self.id_centroid.detach().cpu().numpy() - np.mean(self.id_centroid.detach().cpu().numpy())) ** 2)


    #         # loss = 1 - (id_centroid_sim + class_prob)
    #         # loss = -class_prob + torch.log(id_centroid_dist)
    #         loss = -class_prob
    #         # loss = -rewrite_original_sim + torch.log(id_centroid_dist)
    #         # loss = (1 - ((2 * id_centroid_sim + class_prob) / 3)).squeeze()
    #         # loss = 1 - ((id_centroid_sim + rewrite_original_sim) / 2) # [0, 1] loss with both similarity and rewrite similarity equal
    #         # loss = (1 - ((2 * id_centroid_sim + rewrite_original_sim) / 3)).squeeze() # [0, 1] loss with centroid similarity weighted more
    #         # loss = 2 * id_centroid_dist + rewrite_original_dist # [0, 1] loss with both similarity and rewrite similarity equal
    #         # loss = 1 - class_prob
    #         running_loss = loss if running_loss is None else running_loss + loss
    #         losses.append(loss)

    #     # batch_loss.requires_grad = True
    #     batch_loss = running_loss / len(losses)
    #     model_ref = model(**inputs, labels=inputs["input_ids"]).logits.mean()
    #     model_ref = 0 * model_ref + batch_loss

    #     return (model_ref, generations) if return_outputs else model_ref


    def get_embeddings(self, inputs_batch):
        tokenized_batch = self.sentence_encoder_tokenizer(inputs_batch, return_tensors="pt", padding=True, truncation=True, max_length=512).to("cuda")
        return self.sentence_encoder_model(**tokenized_batch).pooler_output


    def get_class_probs(self, batch_inputs):
        with torch.no_grad():
            tokenized_batch = self.task_tokenizer(batch_inputs, return_tensors="pt", padding=True, truncation=True, max_length=512).to("cuda")
            logits = self.task_model(**tokenized_batch).logits
            probs = torch.softmax(logits, dim=-1)
            return probs


def get_dataset(dataset_name, max_examples):
    local_dataset_paths = {
        "boss_sentiment": {
            "train": "datasets/boss_benchmark/SentimentAnalysis/amazon/train.tsv",
            "test": "datasets/boss_benchmark/SentimentAnalysis/amazon/test.tsv",
        },
        "boss_sentiment_centroid": {
            "train": "datasets/corruped/boss_sentiment_train.csv",
            "test": "datasets/corruped/boss_sentiment_test.csv",
        },
        "boss_toxicity": {
            "train": "datasets/boss_benchmark/ToxicDetection/civil_comments/train.tsv",
            "test": "datasets/boss_benchmark/ToxicDetection/civil_comments/test.tsv",
        },
    }

    if dataset_name in local_dataset_paths:
        train_file_path = local_dataset_paths[dataset_name]["train"]
        seperator = "\t" if "tsv" in train_file_path else ","
        train_set = pd.read_csv(local_dataset_paths[dataset_name]["train"], sep=seperator).dropna()
        train_set.rename(columns={"Text": "text", "Label": "label"}, inplace=True)
        if max_examples is not None:
            train_set = train_set.sample(max_examples)

        test_file_path = local_dataset_paths[dataset_name]["train"]
        test_set = pd.read_csv(local_dataset_paths[dataset_name]["test"], sep=seperator).dropna()
        test_set.rename(columns={"Text": "text", "Label": "label"}, inplace=True)
        if max_examples is not None:
            test_set = test_set.sample(max_examples)

        return DatasetDict(
            {
                "train": Dataset.from_pandas(train_set),
                "test": Dataset.from_pandas(test_set),
            }
        )

    # return load_dataset(dataset_name)
    dataset = get_formatted_dataset(dataset_name, max_examples=None)
    if dataset_name == "sst2":
        dataset["test"] = dataset["validation"]

    if max_examples is not None:
        dataset["train"] = dataset["train"].select(range(max_examples)) if max_examples < len(dataset["train"]) else dataset["train"]
        dataset["test"] = dataset["test"].select(range(max_examples)) if max_examples < len(dataset["test"]) else dataset["test"]

    return dataset


def preprocess_logits_for_metrics(logits, labels):
    predictions = None
    formatted_labels = None
    if not isinstance(logits[0], int):
        model_name = get_cli_args().base_model
        tokenizer = LlamaTokenizer.from_pretrained(model_name) if "llama" in model_name else AutoTokenizer.from_pretrained(model_name)
        formatted_labels = [int(tokenizer.decode([id for id in label_ids if id > 0], skip_special_tokens=True)[-1]) for label_ids in labels]

        raw_predictions = None
        if isinstance(logits, tuple):
            raw_predictions = [tokenizer.decode(word_dist.argmax(-1), skip_special_tokens=True).split("Label:")[-1].split("\n")[0].lower().strip() for word_dist in logits[0]]
        else:
            raw_predictions = [tokenizer.decode(word_dist.argmax(-1), skip_special_tokens=True).split("Label:")[-1].split("\n")[0].lower().strip() for word_dist in logits]

        verbalizers = {
            "pos": 1,
            "positive": 1,
            "1": 1,
            "neg": 0,
            "negative": 0,
            "0": 0,
            "neutral": 2,
            "neut": 2,
            "toxic": 1,
            "non-toxic": 0,
            "word": 0,
            "sports": 1,
            "business": 2,
            "sci/tech": 3,
        }
        predictions = []
        for pred in raw_predictions:
            if pred == "":
                predictions.append(-1)
                continue

            pred = pred.split()[0].lower().strip()
            if pred in verbalizers:
                predictions.append(verbalizers[pred])
            else:
                try:
                    predictions.append(int(pred))
                except:
                    predictions.append(-1)
    else:
        predictions = np.argmax(logits, axis=-1)
        formatted_labels = labels

    return torch.Tensor(predictions), torch.Tensor(formatted_labels)


def compute_metrics(eval_preds):
    predicitons = eval_preds.predictions[0]
    labels = eval_preds.predictions[1]
    report = classification_report(labels, predicitons, output_dict=True)
    return {"eval_f1": report["macro avg"]["f1-score"], "eval_acc": report["accuracy"]}


def tokenize_t5(example, tokenizer):
    inputs = example["text"]
    labels = example["label"]
    source_encodings = tokenizer(inputs, truncation=True, max_length=512, padding="longest")
    target_encodings = tokenizer([str(label) for label in labels], truncation=True, max_length=512, padding="longest")
    source_encodings["labels"] = target_encodings["input_ids"].copy()
    return source_encodings


def tokenize_llm(example, tokenizer, dataset_name, model_name):
    entries = zip(example["text"], example["label"])
    prompts = None
    if "corruped" in dataset_name:
        prompts = example["text"]
    else:
        # exemplars = get_static_exemplars(dataset_name, 8)
        exemplars = []
        # prompts = [f"{generate_classification_prompt(entry[0], exemplars, None, dataset_name)}{entry[1]}{tokenizer.eos_token}" for entry in entries]
        prompts = [generate_rewriter_prompt(dataset_name, model_name, entry[0]) for entry in entries]
    tokenized_input = tokenizer(prompts)
    tokenized_input["labels"] = tokenizer([str(label) for label in example["label"]])["input_ids"]
    return tokenized_input


def generate_rewriter_prompt(dataset_name, model_name, input_text):
    return f"### System:\nParaphrase the following input\n\n### User: {input_text}\n\n### Assistant:"



def get_learning_rate(model_name):
    if is_large_language_model(model_name):
        return 1e-4
    elif is_language_model(model_name):
        return 1e-3
        # return 1e-1
        # return 2e-5
    else:
        return 2e-5


def fine_tune_model():
    args = get_cli_args()
    num_epochs = 20
    dataset_name = args.dataset
    model_name = args.base_model

    experiment_id = f"training_{int(time())}_{args.dataset.replace('/', '_')}_{args.base_model.replace('/', '_')}"
    create_exp_dir(args, experiment_id)

    wandb_run = None
    project_name = None
    if args.use_wandb:
        project_name = "In-Context Domain Transfer Improves Out-of-Domain Robustness"
        wandb_run = wandb.init(project=project_name, group="training", name=experiment_id, config=args)

    dataset = get_dataset(dataset_name, args.max_examples)
    tokenizer, model = get_model_objects(model_name, num_labels=args.num_labels, training=True)
    data_collator = DataCollatorForSeq2Seq(tokenizer, pad_to_multiple_of=8, return_tensors="pt", padding=True)
    if is_large_language_model(model_name):
        peft_config = LoraConfig(task_type=TaskType.CAUSAL_LM, inference_mode=False, r=8, lora_alpha=32, lora_dropout=0.1)
        model = prepare_model_for_int8_training(model)
        model = get_peft_model(model, peft_config)
        model.print_trainable_parameters()

    tokenized_datasets = None
    if is_large_language_model(model_name):
        tokenized_datasets = dataset.map(lambda example: tokenize_llm(example, tokenizer, dataset_name, model_name), batched=True)
    elif is_language_model(model_name):
        tokenized_datasets = dataset.map(lambda example: tokenize_t5(example, tokenizer), batched=True, remove_columns=["text", "label"])
    else:
        tokenized_datasets = dataset.map(lambda example: tokenizer(example["text"], truncation=True, max_length=512), batched=True, remove_columns=["text", "label"])

    for extra_column in ["class", "label"]:
        tokenized_datasets = tokenized_datasets.remove_columns(extra_column) if extra_column in tokenized_datasets["train"].column_names else tokenized_datasets
        # tokenized_datasets = tokenized_datasets.remove_columns(extra_column) if extra_column in tokenized_datasets["validation"].column_names else tokenized_datasets
        tokenized_datasets = tokenized_datasets.remove_columns(extra_column) if extra_column in tokenized_datasets["test"].column_names else tokenized_datasets

    trainer = None
    if is_language_model(model_name):
        trainer = get_seq2seq_trainer(args, num_epochs, experiment_id, project_name, tokenizer, model, data_collator, tokenized_datasets)
    else:
        trainer = get_trainer(args, num_epochs, model_name, experiment_id, project_name, dataset, tokenizer, model, data_collator, tokenized_datasets)

    model.config.use_cache = False
    if is_large_language_model(model_name):
        old_state_dict = model.state_dict
        model.state_dict = (lambda self, *_, **__: get_peft_model_state_dict(self, old_state_dict())).__get__(model, type(model))
        model = torch.compile(model)

    # Train and save the best model and tokenizer to its own directory
    trainer.train()
    trainer.save_model(f"trained_models/{experiment_id}/best_F1={trainer.state.best_metric}")


def get_trainer(args, num_epochs, model_name, experiment_id, project_name, dataset, tokenizer, model, data_collator, tokenized_datasets):
    tokenized_datasets = tokenized_datasets.remove_columns(dataset["train"].column_names)
    training_args = TrainingArguments(
            output_dir=f"trained_models/{experiment_id}/model",
            per_device_train_batch_size=16,
            num_train_epochs=num_epochs,
            weight_decay=0.01,
            learning_rate=get_learning_rate(model_name),
            logging_dir=f"trained_models/{experiment_id}/logs",
            metric_for_best_model="loss" if args.skip_computing_metrics else "eval_f1",
            evaluation_strategy="epoch",
            save_strategy="epoch",
            load_best_model_at_end=True,
            run_name=experiment_id,
            report_to="wandb",
        )

    if args.use_lr_warmup:
        training_args.warmup_ratio = 0.1,

    if is_large_language_model(model_name):
        training_args.fp16 = True

    if args.use_wandb:
        training_args.wandb_project = project_name
        training_args.run_name = experiment_id

    trainer = Trainer(
            model,
            training_args,
            train_dataset=tokenized_datasets["train"],
            eval_dataset=tokenized_datasets["test"],
            data_collator=data_collator,
            tokenizer=tokenizer,
        )

    if not args.skip_computing_metrics:
        print("Adding metrics to trainer")
        trainer.preprocess_logits_for_metrics = preprocess_logits_for_metrics,
        trainer.compute_metrics = compute_metrics
    else:
        print("Skipping metrics")

    return trainer

def get_seq2seq_trainer(args, num_epochs, experiment_id, project_name, tokenizer, model, data_collator, tokenized_datasets):
    training_args = Seq2SeqTrainingArguments(
            output_dir=f"trained_models/{experiment_id}/model",
            per_device_train_batch_size=4,
            num_train_epochs=num_epochs,
            weight_decay=0.01,
            learning_rate=get_learning_rate(args.base_model),
            logging_dir=f"trained_models/{experiment_id}/logs",
            metric_for_best_model="loss" if args.skip_computing_metrics else "eval_f1",
            evaluation_strategy="epoch",
            save_strategy="epoch",
            load_best_model_at_end=True,
            warmup_ratio = 0.1,
        )

    if args.use_wandb:
        training_args.wandb_project = project_name
        training_args.run_name = experiment_id

    if "__index_level_0__" in tokenized_datasets["train"].column_names:
        tokenized_datasets["train"] = tokenized_datasets["train"].remove_columns(["__index_level_0__"])
    if "__index_level_0__" in tokenized_datasets["test"].column_names:
        tokenized_datasets["test"] = tokenized_datasets["test"].remove_columns(["__index_level_0__"])

    trainer = RewriteTrainer(
            model,
            training_args,
            train_dataset=tokenized_datasets["train"],
            eval_dataset=tokenized_datasets["test"],
            data_collator=data_collator,
            tokenizer=tokenizer,
            preprocess_logits_for_metrics=None if args.skip_computing_metrics else preprocess_logits_for_metrics,
            compute_metrics=None if args.skip_computing_metrics else compute_metrics
        )

    # trainer.sentence_encoder_pipeline = pipeline("feature-extraction", model="princeton-nlp/sup-simcse-roberta-large", device=model.device)

    # if not args.skip_computing_metrics:
    #     print("Adding metrics to trainer")
    #     trainer.preprocess_logits_for_metrics = preprocess_logits_for_metrics,
    #     trainer.compute_metrics = compute_metrics
    # else:
    #     print("Skipping metrics")

    return trainer


def get_cli_args():
    parser = ArgumentParser()
    parser.add_argument("--dataset", type=str, required=True)
    parser.add_argument("--num_labels", type=int, required=True)
    parser.add_argument("--base_model", type=str, required=False, default="Kyle1668/boss-sentiment-bert-base-uncased")
    parser.add_argument("--max_examples", type=int, required=False, default=None)
    parser.add_argument("--use_lr_warmup", action="store_true")
    parser.add_argument("--skip_computing_metrics", action="store_true")
    parser.add_argument("--use_wandb", action="store_true")
    args = parser.parse_args()
    return args


def create_exp_dir(args, experiment_id):
    if not os.path.exists("trained_models"):
        os.mkdir("trained_models")
    os.mkdir(f"trained_models/{experiment_id}")
    json.dump(vars(args), open(f"trained_models/{experiment_id}/config.json", "w"), indent=4)


if __name__ == "__main__":
    fine_tune_model()
