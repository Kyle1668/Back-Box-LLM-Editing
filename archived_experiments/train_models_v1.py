from peft import get_peft_config, get_peft_model, LoraConfig, TaskType
from sklearn.metrics import classification_report
from datasets import Dataset, DatasetDict
from argparse import ArgumentParser
from torch.utils.data import DataLoader
from time import time
from tqdm import tqdm
import pandas as pd
import numpy as np
import json
import os
import torch
import wandb

from util_modeling import get_model_objects, is_language_model, is_large_language_model
from util_data import get_formatted_dataset
from adaptive_methods import GenericDataset


def get_dataset(dataset_name):
    local_dataset_paths = {
        "boss_sentiment": {
            "train": "datasets/boss_benchmark/SentimentAnalysis/amazon/train.tsv",
            "test": "datasets/boss_benchmark/SentimentAnalysis/amazon/test.tsv",
        },
        "boss_toxicity": {
            "train": "datasets/boss_benchmark/ToxicDetection/civil_comments/train.tsv",
            "test": "datasets/boss_benchmark/ToxicDetection/civil_comments/test.tsv",
        },
    }

    if dataset_name in local_dataset_paths:
        train_set = pd.read_csv(local_dataset_paths[dataset_name]["train"], sep="\t").dropna()
        train_set.rename(columns={"Text": "text", "Label": "label"}, inplace=True)
        test_set = pd.read_csv(local_dataset_paths[dataset_name]["test"], sep="\t").dropna()
        test_set.rename(columns={"Text": "text", "Label": "label"}, inplace=True)
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

    return dataset


def train_model(model, tokenizer, training_set):
    prepped_train_set = GenericDataset(training_set)
    batch_size = 3 if is_language_model(model.name_or_path) else 32
    training_loader = DataLoader(prepped_train_set, batch_size=batch_size, shuffle=True)
    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-5)
    criterion = torch.nn.CrossEntropyLoss()
    is_lm = is_language_model(model.name_or_path)

    # Train model
    batch_losses = []
    for input_batch, label_batch in tqdm(training_loader, desc="Training Model"):
        model.train()
        optimizer.zero_grad()

        labels = None
        tokenized_inputs = None
        if is_large_language_model(model.name_or_path):
            labels = tokenizer([f"{entry[0]} {entry[1]}" for entry in zip(input_batch, label_batch)], return_tensors="pt", padding=True, truncation=True, max_length=512) if is_lm else label_batch
            labels = labels.to(model.device)
            tokenized_inputs = labels
            labels = labels.input_ids
        else:
            labels = tokenizer([f"{entry[0]} {entry[1]}" for entry in zip(input_batch, label_batch)], return_tensors="pt", padding=True, truncation=True, max_length=512) if is_lm else label_batch
            labels = labels.input_ids if is_lm else labels
            labels = labels.to(model.device)

            tokenized_inputs = tokenizer(input_batch, padding=True, truncation=True, return_tensors="pt").to(model.device) if not is_large_language_model(model.name_or_path) else labels
        loss = model(**tokenized_inputs, labels=labels).loss
        loss.backward()
        optimizer.step()
        batch_losses.append(loss.detach().item())

    return sum(batch_losses) / len(batch_losses)


def evaluate_model(experiment_id, dataset_name, model, tokenizer, test_set, epoch) -> float:
    model.eval()
    prepped_test_set = GenericDataset(test_set)
    test_loader = DataLoader(prepped_test_set, batch_size=1, shuffle=True)
    is_lm = is_language_model(model.name_or_path)

    predicitons = []
    labels = []
    for eval_text, eval_labels in tqdm(test_loader, desc="Evaluating Model"):
        with torch.no_grad():
            tokenized_input = tokenizer(eval_text, padding=True, truncation=True, return_tensors="pt", max_length=512).to(model.device)

            if is_lm:
                generations = model.generate(**tokenized_input, do_sample=False, max_new_tokens=50)
                decoded_generations = tokenizer.batch_decode(generations, skip_special_tokens=True)[len(tokenized_input["input_ids"]):]
                if len(decoded_generations) == 0:
                    decoded_generations = ["_"]

                eval_predicitons = [chars[0] if len(chars) > 0 else chars for chars in decoded_generations]
                eval_predicitons = [int(pred) if pred in ["0", "1", "2", "3", "4"] else -1 for pred in eval_predicitons]
                predicitons += eval_predicitons
                labels += [str(label) for label in eval_labels.tolist()]
            else:
                eval_logits = model(**tokenized_input).logits
                eval_predicitons = torch.argmax(eval_logits, dim=1)
                predicitons += eval_predicitons.tolist()
                labels += eval_labels.tolist()

    string_labels = [str(label) for label in labels]
    string_predicitons = [str(pred) for pred in predicitons]
    print(classification_report(string_labels, string_predicitons))
    report = classification_report(string_labels, string_predicitons, output_dict=True)

    # Save report
    if not os.path.exists("trained_models"):
        os.mkdir("trained_models")
    if not os.path.exists(f"trained_models/{experiment_id}"):
        os.mkdir(f"trained_models/{experiment_id}")
    if not os.path.exists(f"trained_models/{experiment_id}/{dataset_name}"):
        os.mkdir(f"trained_models/{experiment_id}/{dataset_name}")

    model_name = model.config.name_or_path.replace("/", "_")
    os.mkdir(f"trained_models/{experiment_id}/{dataset_name}/{model_name}_{epoch}")
    model.save_pretrained(f"trained_models/{experiment_id}/{dataset_name}/{model_name}_{epoch}")
    tokenizer.save_pretrained(f"trained_models/{experiment_id}/{dataset_name}/{model_name}_{epoch}")
    with open(f"trained_models/{experiment_id}/{dataset_name}/{model_name}_{epoch}/report.json", "w") as f:
        json.dump(report, f, indent=4)

    return report


def main():
    parser = ArgumentParser()
    parser.add_argument("--dataset", type=str, required=True)
    parser.add_argument("--num_labels", type=int, required=True)
    parser.add_argument("--base_model", type=str, required=False, default="bert-base-uncased")
    parser.add_argument("--max_examples", type=int, required=False, default=None)
    parser.add_argument("--use_wandb", action="store_true")
    args = parser.parse_args()

    experiment_id = f"training_{int(time())}_{args.dataset}_{args.base_model.replace('/', '_')}"
    num_epochs = 20
    dataset_name = args.dataset
    model_name = args.base_model
    if not os.path.exists("trained_models"):
        os.mkdir("trained_models")
    os.mkdir(f"trained_models/{experiment_id}")
    json.dump(vars(args), open(f"trained_models/{experiment_id}/config.json", "w"), indent=4)

    wandb_run = None
    if args.use_wandb:
        project_name = "In-Context Domain Transfer Improves Out-of-Domain Robustness"
        wandb_run = wandb.init(project=project_name, group="training", name=experiment_id, config=args)

    dataset = get_dataset(dataset_name)
    tokenizer, model = get_model_objects(model_name, num_labels=args.num_labels, training=True)
    if is_large_language_model(model_name):
        peft_config = LoraConfig(task_type=TaskType.CAUSAL_LM, inference_mode=False, r=8, lora_alpha=32, lora_dropout=0.1)
        model = get_peft_model(model, peft_config)
        model.print_trainable_parameters()

    training_set = dataset["train"][: args.max_examples] if args.max_examples is not None else dataset["train"]
    test_set = dataset["test"][: args.max_examples] if args.max_examples is not None else dataset["test"]
    train_losses = []
    epoch_reports = []
    epoch_accuracies = []

    print(f"Training {model_name} on {dataset_name} for {num_epochs} epochs")
    for epoch in range(num_epochs):
        print(f"Epoch {epoch}")
        train_loss = train_model(model, tokenizer, training_set)
        train_losses.append(train_loss)
        if args.use_wandb:
            wandb.log({"train_loss": train_loss})


        if epoch % 5 == 0:
            test_set_perf_report = evaluate_model(experiment_id, dataset_name, model, tokenizer, test_set, epoch)
            formatted_report = (
                test_set_perf_report
                if not is_language_model(model_name)
                else {str(label): test_set_perf_report[str(label)] for label in range(args.num_labels)} | {"accuracy": test_set_perf_report["accuracy"], "macro avg": test_set_perf_report["macro avg"]}
            )

            epoch_reports.append(formatted_report)
            epoch_accuracies.append(formatted_report["accuracy"])
            if args.use_wandb:
                wandb.log(formatted_report)
                table = wandb.Table(dataframe=pd.DataFrame(epoch_reports))
                wandb_run.log({"test_set_reports": table})

    print(epoch_accuracies)
    highest_acc_epoch = np.argmax(epoch_accuracies)
    print(f"Highest accuracy (epoch {highest_acc_epoch}): {epoch_accuracies[highest_acc_epoch]}")


if __name__ == "__main__":
    main()
