from sklearn.metrics import classification_report, confusion_matrix, ConfusionMatrixDisplay
from datasets import load_dataset, Dataset, DatasetDict
from metrics_util import SquadMetrics
from datasets import load_dataset
from wilds import get_dataset
from tqdm import tqdm
import pandas as pd
import numpy as np
import json
import os


def qa_report(model_answers, gold_answers):
    f1s = ems = []
    for model_answer, gold_answer in zip(model_answers, gold_answers):
        f1s.append(SquadMetrics.f1_score(model_answer, gold_answer))
        ems.append(SquadMetrics.exact_match_score(model_answer, gold_answer))

    mean_f1 = np.mean(f1s)
    exact_match_rate = np.sum(ems) / len(ems)
    return { "f1-score": mean_f1, "exact match rate": exact_match_rate }


def generate_icl_report(experiment_id, model_name, dataset_name, icl_method, eval_set, dataset, data_reader, original_judgments, adaptive_model_name, num_shots, num_failed_generations):
    if not os.path.exists(f"results/{experiment_id}"):
        os.makedirs(f"results/{experiment_id}")

    is_qa_task = dataset_name.startswith("squad")
    formatted_model_name = model_name.replace("/", "-")
    gold_labels = [entry["label"] for entry in dataset[eval_set.replace("+adaptive", "")]]
    report_dict = qa_report(original_judgments, gold_labels) if is_qa_task else classification_report(gold_labels, original_judgments, output_dict=True)
    reported_name_map = {
        "validation": "In-Distribution",
        "test": "Out-of-Distribution",
        "test+adaptive": "OOD w/ Style Transfer"
    }
    icl_report = {
        "dataset": dataset_name,
        "split": reported_name_map[eval_set],
        "dataset size": len(dataset[eval_set.replace("+adaptive", "")]),
        "icl_method": icl_method,
        "task model": formatted_model_name,
        "style transfer model": adaptive_model_name if eval_set == "test+adaptive" else None,
        "exemplar count": num_shots,
        "accuracy": report_dict["accuracy"] if not is_qa_task else None,
        "avg precision": report_dict["macro avg"]["precision"] if not is_qa_task else None,
        "avg recall": report_dict["macro avg"]["recall"] if not is_qa_task else None,
        "avg f1": report_dict["macro avg"]["f1-score"] if not is_qa_task else report_dict["f1-score"],
        "num failed generations": num_failed_generations,
        "exact match rate": report_dict["exact match rate"] if is_qa_task else None,
    }
    output_file_name = f"set={dataset_name}_split={eval_set}_method={icl_method}_model={formatted_model_name}"

    if eval_set == "prod":
        json.dump(icl_report, open(f"results/{experiment_id}/{output_file_name}_report.json", "w+"), indent=4)
        print(f"Classification Results: {formatted_model_name} {dataset_name} {icl_method}")
        print(classification_report(data_reader.references, original_judgments))
        confusion_matrix_fig = ConfusionMatrixDisplay(confusion_matrix=confusion_matrix(data_reader.references, original_judgments))
        confusion_matrix_fig.figure_.savefig(f"results/{experiment_id}/{output_file_name}_confusion_matrix.png")

    return icl_report


def get_formatted_dataset(set_name, max_examples=None):
    hf_paths = {"sst2": "gpt3mix/sst2", "toxigen": "skg/toxigen-data", "disaster_tweets": "venetis/disaster_tweets"}
    hf_sets_columns_mappings = {
        "toxigen": ("prompt", "prompt_label"),
        "disaster_tweets": ("text", "target"),
        "amazon_polarity": ("content", "label"),
        "imdb": ("text", "label"),
        "adv_sst2": ("sentence", "label"),
        "sst2": ("sentence", "label"),
        "ag_news": ("text", "label"),
        "squad": ("context", "answers", "question"),
    }

    hf_dataset = None
    hf_path = hf_paths[set_name] if set_name in hf_paths else set_name
    if set_name.startswith("wilds_"):
        hf_dataset = load_wilds_dataset(hf_path)
    elif set_name == "scotus":
        hf_dataset = load_scotus_dataset()
    elif set_name == "ag_news":
        hf_dataset = load_shifted_agnews_dataset()
    elif set_name == "civil_toxigen":
        hf_dataset = load_civil_comments_and_toxigen_dataset()
    elif set_name == "adv_sst2":
        hf_dataset = load_adv_sst2()
    elif set_name == "rotten_tomatoes_imdb":
        hf_dataset = DatasetDict({"train": load_dataset("rotten_tomatoes", split="train"), "test": load_dataset("imdb", split="test")})
    elif set_name == "imdb_rotten_tomatoes":
        hf_dataset = DatasetDict({"train": load_dataset("imdb", split="test"), "test": load_dataset("rotten_tomatoes", split="test")})
    elif set_name.startswith("squadshifts_"):
        test_set_name = set_name.split("_")[1]
        train_set = load_dataset("squad", split="train")
        validaiton_set = load_dataset("squad", split="validation")
        test_set = load_dataset("squadshifts", test_set_name, split="test")
        hf_dataset = DatasetDict({"train": train_set, "validation": validaiton_set, "test": test_set})
    else:
        hf_dataset = load_dataset(hf_path)

    is_qa_task = "squad" in set_name
    set_name = "squad" if set_name.startswith("squadshifts_") else set_name
    for split in hf_dataset.keys():
        if "text" not in hf_dataset[split][0].keys():
            hf_dataset[split] = hf_dataset[split].rename_column(hf_sets_columns_mappings[set_name][0], "text")
        if "label" not in hf_dataset[split][0].keys():
            hf_dataset[split] = hf_dataset[split].rename_column(hf_sets_columns_mappings[set_name][1], "label")
        if is_qa_task:
            # hf_dataset["train"] = hf_dataset["train"].rename_column(hf_sets_columns_mappings[set_name][2], "question")
            # hf_dataset["test"] = hf_dataset["test"].rename_column(hf_sets_columns_mappings[set_name][2], "question")
            # For Q&A tasks, the label columns may have multiple answers, so we need to convert them to a single answer
            # TODO: Verify best way to combine answers: " ".join(hf_dataset["test"][0]["label"]["text"])
            hf_dataset[split] = hf_dataset[split].map(lambda x: {"label": x["label"]["text"][0]})

    # Create a validation set from the same dist as the train set - if none already exist
    if "validation" not in hf_dataset.keys():
        train_set = hf_dataset["train"].to_pandas()
        validation_set = train_set.sample(frac=0.2)
        train_set = train_set.drop(validation_set.index)
        hf_dataset["train"] = Dataset.from_pandas(train_set)
        hf_dataset["validation"] = Dataset.from_pandas(validation_set)

    if max_examples is not None:
        for split in ["train", "validation", "test"]:
            if max_examples >= len(hf_dataset[split]):
                print(f"WARNING: max_examples ({max_examples}) is greater than the number of examples in the {split} set ({len(hf_dataset[split])}).")
                continue

            new_frame = None
            split_frame = hf_dataset[split].to_pandas()
            if is_qa_task:
                new_frame = split_frame.sample(max_examples)
            else:
                labels = split_frame["label"].unique()
                max_examples_per_label = max_examples // len(labels)
                for label in labels:
                    current_label_sample_size = max_examples_per_label if len(split_frame[split_frame["label"] == label]) > max_examples_per_label else len(split_frame[split_frame["label"] == label])
                    label_samples = split_frame[split_frame["label"] == label].sample(current_label_sample_size)
                    if new_frame is None:
                        new_frame = label_samples
                    else:
                        new_frame = pd.concat([new_frame, label_samples])

            new_frame = new_frame.sample(frac=1)
            new_frame = new_frame.drop(columns=["__index_level_0__"]) if "__index_level_0__" in new_frame.columns else new_frame
            hf_dataset[split] = Dataset.from_pandas(new_frame)

    # Split the test set into a production traffic set from which edits will be made, and a holdout set
    if enable_edits := False:
        original_test_set = hf_dataset["test"].to_pandas().drop(columns=["__index_level_0__"])
        edit_set = original_test_set.sample(frac=0.5)
        test_set = original_test_set.drop(edit_set.index)
        hf_dataset["test"] = Dataset.from_pandas(test_set)
        hf_dataset["prod"] = Dataset.from_pandas(edit_set)

    return hf_dataset


def load_civil_comments_and_toxigen_dataset() -> DatasetDict:
    civil_comments = load_wilds_dataset("wilds_civil_comments")
    toxigen = load_dataset("skg/toxigen-data", "train", use_auth_token=True).rename_column("generation", "text").rename_column("prompt_label", "label")
    formatted_toxigen = toxigen["train"].map(lambda x: {"text": x["text"].replace("- ", "").split("\\n")[0]})
    return DatasetDict(
        {
            "train": formatted_toxigen,
            "test": civil_comments["test"],
        }
    )


def load_adv_sst2() -> DatasetDict:
    original_dist_train = load_dataset("sst2", split="train")
    original_dist_eval = load_dataset("sst2", split="validation")
    adversarial_dist = load_dataset("adv_glue", "adv_sst2")["validation"]
    return DatasetDict(
        {
            "train": original_dist_train,
            "validation": original_dist_eval,
            "test": adversarial_dist,
        }
    )


def load_scotus_dataset():
    train_set = pd.read_csv("datasets/scotus_train.csv")
    test_set = pd.read_csv("datasets/scotus_test.csv")
    full_dataset = DatasetDict()
    full_dataset["train"] = Dataset.from_pandas(train_set)
    full_dataset["test"] = Dataset.from_pandas(test_set)
    return full_dataset


def load_shifted_agnews_dataset():
    full_dataset = DatasetDict()
    full_dataset["train"] = Dataset.from_pandas(pd.read_csv("datasets/ag_news_train.csv"))
    full_dataset["test"] = load_dataset("ag_news", split="test")
    return full_dataset


def load_wilds_dataset(dataset_name):
    if dataset_name == "wilds_civil_comments":
        dataset = get_dataset(dataset="civilcomments", download=True)
        train_dict = {"text": [], "label": [], "group": []}
        for text, label, group in dataset.get_subset("train"):
            train_dict["text"].append(text)
            train_dict["label"].append(label.item())
            train_dict["group"].append(group.tolist())

        test_dict = {"text": [], "label": [], "group": []}
        for text, label, group in dataset.get_subset("test"):
            test_dict["text"].append(text)
            test_dict["label"].append(label.item())
            test_dict["group"].append(group.tolist())

        full_dataset = DatasetDict()
        full_dataset["train"] = Dataset.from_pandas(pd.DataFrame(train_dict))
        full_dataset["test"] = Dataset.from_pandas(pd.DataFrame(test_dict))
        return full_dataset
    elif dataset_name == "wilds_amazon":
        dataset = get_dataset(dataset="amazon", download=True)
        train_dict = {"text": [], "label": [], "group": []}
        for content, label, group in dataset.get_subset("train"):
            train_dict["text"].append(content)
            train_dict["label"].append(label.item())
            train_dict["group"].append(group.tolist())

        test_dict = {"text": [], "label": [], "group": []}
        for content, label, group in dataset.get_subset("test"):
            test_dict["text"].append(content)
            test_dict["label"].append(label.item())
            test_dict["group"].append(group.tolist())

        full_dataset = DatasetDict()
        full_dataset["train"] = Dataset.from_pandas(pd.DataFrame(train_dict))
        full_dataset["test"] = Dataset.from_pandas(pd.DataFrame(test_dict))
        return full_dataset
    else:
        raise Exception("Invalid WILDS dataset")
