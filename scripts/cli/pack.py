import glob
import hashlib
import json
import zipfile
from argparse import ArgumentParser, Namespace
from os import listdir, makedirs
from os.path import basename, dirname, exists, isfile, join
from typing import List, Mapping

import ruamel.yaml
from colorama import Fore, init
from PyInquirer import prompt
from transformers import ADAPTER_CONFIG_MAP, WEIGHTS_NAME, AutoModel
from transformers.adapter_utils import ADAPTER_CACHE
from transformers.adapter_utils import WEIGHTS_NAME as ADAPTER_WEIGHTS_NAME
from transformers.adapter_utils import download_cached, get_adapter_config_hash
from transformers.commands import BaseTransformersCLICommand


ADAPTER_TEMPLATE_YAML = "https://raw.githubusercontent.com/Adapter-Hub/Hub/master/TEMPLATES/adapter.template.yaml"

ADAPTER_KEYS_TO_COPY = ["type", "model_type", "model_name", "model_class"]

ADAPTER_PACK_METADATA_FILE = join(ADAPTER_CACHE, "pack_metadata.json")

DEFAULT_OUTPUT_PATH = ".PACK_OUTPUT"


def adapter_pack_command_factory(args: Namespace):
    return AdapterPackCommand(args.input_paths, args.output_path, args.template, not args.no_extract)


class AdapterPackCommand(BaseTransformersCLICommand):
    @staticmethod
    def register_subcommand(parser: ArgumentParser):
        extract_parser = parser.add_parser(
            "pack",
            help="CLI tool to extract all adapters in a directory and to prepare them for upload to AdapterHub.",
        )
        extract_parser.add_argument(
            "input_paths", metavar="PATH", type=str, nargs="+", help="Path to a directory pretrained models or pretrained adapters."
        )
        extract_parser.add_argument(
            "-o",
            "--output_path",
            type=str,
            help="Path to a directory where the packed adapters will be saved. Will save to .PACK_OUTPUT folder by default.",
        )
        extract_parser.add_argument(
            "--template", type=str, help="Path to a YAML file to be used as template for the adapter info cards."
        )
        extract_parser.add_argument(
            "--no_extract",
            action="store_true",
            help="Don't attempt to extract from models found in the input directory.",
        )
        extract_parser.set_defaults(func=adapter_pack_command_factory)

    def __init__(
        self,
        input_paths: List[str],
        output_path: str,
        template: str,
        extract_from_models: bool = False,
    ):
        self.input_paths = input_paths
        self.output_path = output_path or DEFAULT_OUTPUT_PATH
        self.template = template
        self.extract_from_models = extract_from_models
        self._validate_func = lambda x: len(x) > 0 or "This field must not be empty."
        self._input_cache = {}
        # create a lookup map for default configs
        self._config_id_lookup = {}
        for k, v in ADAPTER_CONFIG_MAP.items():
            config_hash = get_adapter_config_hash(v)
            self._config_id_lookup[config_hash] = k

    def find_models_and_adapters(self) -> List:
        # full models and adapters are identified by their weights file
        models_list = []
        if self.extract_from_models:
            for input_path in self.input_paths:
                for weights_file in glob.glob(join(input_path, "**", WEIGHTS_NAME), recursive=True):
                    models_list.append(dirname(weights_file))
        adapters_list = []
        for input_path in self.input_paths:
            for weights_file in glob.glob(join(input_path, "**", ADAPTER_WEIGHTS_NAME), recursive=True):
                adapters_list.append(dirname(weights_file))
        return models_list, adapters_list

    def ask_for_metadata(self) -> Mapping:
        metadata_config = {}
        if isfile(ADAPTER_PACK_METADATA_FILE):
            with open(ADAPTER_PACK_METADATA_FILE, "r") as f:
                metadata_config = json.load(f)
        inputs = [
            {
                "type": "input",
                "name": "author",
                "message": "Your name or the names of other authors:",
                "validate": self._validate_func,
                "default": metadata_config.get("author", ""),
            },
            {
                "type": "input",
                "name": "email",
                "message": "An email address to contact you or other authors:",
                "validate": self._validate_func,
                "default": metadata_config.get("email", ""),
            },
            {
                "type": "input",
                "name": "url",
                "message": "[opt.] A URL providing more information on you or your work:",
                "default": metadata_config.get("url", ""),
            },
            {
                "type": "input",
                "name": "twitter",
                "message": "[opt.] Your or your organization's Twitter handle:",
                "default": metadata_config.get("twitter", ""),
            },
            {
                "type": "input",
                "name": "github",
                "message": "[opt.] Your or your organization's GitHub account:",
                "default": metadata_config.get("github", ""),
            },
            {
                "type": "input",
                "name": "adapter_url_template",
                "message": "[opt.] A template for the download links of your adapters:",
                "default": metadata_config.get("adapter_url_template", ""),
            },
        ]
        answers = prompt(inputs)
        with open(ADAPTER_PACK_METADATA_FILE, "w") as f:
            json.dump(answers, f)
        return answers

    def ask_for_adapter_type(self) -> Mapping:
        inputs = [
            {
                "type": "list",
                "name": "type",
                "message": "The type of adapter:",
                "choices": [
                    {"value": "text_task", "name": "Task"},
                    {"value": "text_lang", "name": "Language"},
                ],
            }
        ]
        answers = prompt(inputs)
        return answers

    def ask_for_adapter_data(self) -> Mapping:
        inputs = [
            {
                "type": "input",
                "name": "task",
                "message": "The identifier of the task:",
                "validate": self._validate_func,
            },
            {
                "type": "input",
                "name": "subtask",
                "message": "The identifier of the subtask:",
                "validate": self._validate_func,
            },
        ]
        answers = prompt(inputs)
        return answers

    def ask_for_adapter_config(self, config_id):
        inputs = [
            {
                "type": "input",
                "name": "config_name",
                "message": "The name of the adapter config:",
                "validate": self._validate_func,
                "default": self._config_id_lookup.get(config_id, ""),
            },
        ]
        answers = prompt(inputs)
        return answers

    def ask_for_model_name(self):
        print("[i] We couldn't find a model name in this adapter's config. Please specify one.")
        inputs = [
            {
                "type": "input",
                "name": "model_name",
                "message": "Identifier of the pre-trained model:",
                "validate": self._validate_func,
                "default": self._input_cache.get("model_name", ""),
            }
        ]
        answers = prompt(inputs)
        self._input_cache["model_name"] = answers["model_name"]
        return answers

    def pack_adapter(
        self, folder, save_root, template_file, adapter_data=None, metadata=None, version="1", url_template=None
    ):
        adapter_data = adapter_data or {}
        # load config from folder and add it to data
        with open(join(folder, "adapter_config.json"), "r") as f:
            config = json.load(f)
        for k, v in config.items():
            adapter_data[k] = v
        # ask for type if not given
        if not adapter_data.get("type", None):
            for k, v in self.ask_for_adapter_type().items():
                adapter_data[k] = v
        # ask for data from user if not given
        for k, v in self.ask_for_adapter_data().items():
            adapter_data[k] = v
        # ask for config name
        for k, v in self.ask_for_adapter_config(adapter_data["config_id"]).items():
            adapter_data[k] = v
        # ask for model name if we couldn't find it
        if not adapter_data.get("model_name", None):
            for k, v in self.ask_for_model_name().items():
                adapter_data[k] = v
        # zip adapter folder to destination
        folder_name = "_".join(
            [adapter_data["model_name"], adapter_data["task"], adapter_data["subtask"], adapter_data["config_name"]]
        )
        folder_name = folder_name.replace("/", "-")
        zip_name = join(save_root, "{}.zip".format(folder_name))
        print("Zipping {} to {}...".format(folder, zip_name))
        zipf = zipfile.ZipFile(zip_name, "w")
        for file in listdir(folder):
            zipf.write(join(folder, file), arcname=file)
        zipf.close()
        # add description file
        # calculate the hash of the zip file
        with open(zip_name, "rb") as f:
            file_bytes = f.read()
            sha1 = hashlib.sha1(file_bytes).hexdigest()
            sha256 = hashlib.sha256(file_bytes).hexdigest()
        # use url template if available
        if url_template:
            download_url = url_template.format(file=basename(zip_name), **adapter_data)
        else:
            download_url = "TODO"
        file_info = {"version": version, "url": download_url, "sha1": sha1, "sha256": sha256}
        # load the template and fill in data
        yaml = ruamel.yaml.YAML()
        with open(template_file, "r") as f:
            template = yaml.load(f)
        for key in adapter_data:
            if key in template and key != "config":
                template[key] = adapter_data[key]
        template["files"] = [file_info]
        template["default_version"] = version
        template["config"] = {
            "using": adapter_data["config_name"],
            "non_linearity": adapter_data["config"]["non_linearity"],
            "reduction_factor": adapter_data["config"]["reduction_factor"],
        }
        template["prediction_head"] = exists(join(folder, "head_config.json"))
        # optionally add provided metadata
        if metadata:
            for k, v in metadata.items():
                template[k] = v
        # save and finish
        with open(join(save_root, "{}.yaml".format(folder_name)), "w") as f:
            yaml.dump(template, f)
        print(Fore.GREEN + f"✔ Created info card for {folder_name}")

    def print_finalization_text(self):
        print(Fore.CYAN + "=" * 10 + " Step 3: Finalization & Upload " + "=" * 10)
        print("Great! We have successfully packed all adapters.")
        print("These are your next steps for publishing them on AdapterHub:")
        print(Fore.MAGENTA + "-> Upload the created zip folders to your server.")
        print(Fore.MAGENTA + "-> Add the download links to your adapters to the respective yaml info cards.")
        print(
            Fore.MAGENTA
            + "-> Add additional information to your info cards. Description and citation are very useful!"
        )
        print(Fore.MAGENTA + "-> Open a pull request to https://github.com/adapter-hub/Hub to add your info cards.")
        print()

    def pack_adapters_interactive(self, folders, save_root, version="1"):
        print(Fore.CYAN + "=" * 10 + f" Step 2: Packing {len(folders)} adapters " + "=" * 10)
        if not self.template:
            print("Downloading template...")
            template_file = download_cached(ADAPTER_TEMPLATE_YAML)
        elif not isfile(self.template):
            print(Fore.RED + "ERROR: Specified template file could not be found!")
            exit(1)
        else:
            template_file = self.template
        # create target folder if not existent
        if not exists(save_root):
            makedirs(save_root)
        # ask for metadata
        print("[i] Before we start packing your adapters, we first need some meta information.")
        print("This information will be added to every adapter info card.")
        print("Prompts starting with '[opt.]' are optional. Skip them if not applicable.")
        metadata = self.ask_for_metadata()
        url_template = metadata.pop("adapter_url_template")
        print("Thanks! Now let's start...")
        for i, folder in enumerate(folders):
            print(Fore.CYAN + f"[Adapter {i+1} of {len(folders)}] {folder}")
            try:
                self.pack_adapter(
                    folder, save_root, template_file, metadata=metadata, version=version, url_template=url_template
                )
            except Exception as ex:
                print(Fore.RED + "✘ Failed to pack adapter")
                print(Fore.RED + str(ex))
        self.print_finalization_text()

    def run(self):
        init(autoreset=True)  # init colorama
        models_list, adapters_list = self.find_models_and_adapters()
        print(Fore.CYAN + "=" * 10 + " Step 1: Found models and adapters " + "=" * 10)
        for model_dir in models_list:
            print(f"Model in {model_dir}")
        for adapter_dir in adapters_list:
            print(f"Adapter in {adapter_dir}")
        answers = prompt(
            [
                {
                    "type": "list",
                    "name": "mode",
                    "message": "What would you like to do?",
                    "choices": [
                        {"value": "extract", "name": "Extract adapters from found models & pack all found adapters"},
                        {"value": "no_extract", "name": "Pack all found adapters"},
                        {"value": "exit", "name": "Exit"},
                    ],
                }
            ]
        )
        if answers["mode"] == "exit":
            return
        # extract adapters from all models
        elif answers["mode"] == "extract":
            for model_dir in models_list:
                print(f"Extracting adapters from model in {model_dir} ...")
                # TODO use the correct model class
                model = AutoModel.from_pretrained(model_dir)
                for adapter_path in model.save_all_adapters(model_dir):
                    if adapter_path not in adapters_list:
                        adapters_list.append(adapter_path)
        self.pack_adapters_interactive(adapters_list, self.output_path)
