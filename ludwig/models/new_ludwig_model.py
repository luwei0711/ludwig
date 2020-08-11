import copy
import logging
import os
from pprint import pformat

import pandas as pd
import tensorflow as tf
import yaml

from ludwig.constants import TRAINING, VALIDATION, TEST
from ludwig.contrib import contrib_command
from ludwig.data.preprocessing import load_metadata, preprocess_for_training
from ludwig.features.feature_utils import update_model_definition_with_metadata
from ludwig.globals import set_disable_progressbar, \
    MODEL_HYPERPARAMETERS_FILE_NAME, MODEL_WEIGHTS_FILE_NAME, \
    TRAIN_SET_METADATA_FILE_NAME, set_on_master, is_on_master
from ludwig.models.ecd import ECD
from ludwig.models.trainer import Trainer
from ludwig.modules.metric_modules import get_best_function
from ludwig.utils.data_utils import load_json, save_json
from ludwig.utils.defaults import default_random_seed, merge_with_defaults
from ludwig.utils.horovod_utils import should_use_horovod
from ludwig.utils.misc_utils import get_experiment_dir_name, get_file_names, \
    get_experiment_description
from ludwig.utils.print_utils import print_boxed
from ludwig.utils.tf_utils import initialize_tensorflow

logger = logging.getLogger(__name__)


class NewLudwigModel:

    def __init__(self,
                 model_definition=None,
                 model_definition_file=None,
                 logging_level=logging.ERROR,
                 use_horovod=False,
                 gpus=None,
                 gpu_memory_limit=None,
                 allow_parallel_threads=True,
                 random_seed=default_random_seed):
        # check for model_definition and model_definition_file
        if model_definition is None and model_definition_file is None:
            raise ValueError(
                'Either model_definition of model_definition_file have to be'
                'not None to initialize a LudwigModel'
            )
        if model_definition is not None and model_definition_file is not None:
            raise ValueError(
                'Only one between model_definition and '
                'model_definition_file can be provided'
            )

        # merge model definition with defaults
        if model_definition_file is not None:
            with open(model_definition_file, 'r') as def_file:
                raw_model_definition = yaml.safe_load(def_file)
        else:
            raw_model_definition = copy.deepcopy(model_definition)
        self.model_definition = merge_with_defaults(raw_model_definition)

        # setup horovod
        self._horovod = None
        if should_use_horovod(use_horovod):
            import horovod.tensorflow
            self._horovod = horovod.tensorflow
            self._horovod.init()

        # setup logging
        self.set_logging_level(logging_level)

        # setup TensorFlow
        initialize_tensorflow(gpus, gpu_memory_limit, allow_parallel_threads,
                              self._horovod)
        tf.random.set_seed(random_seed)

        # setup model
        self.model = None
        self.train_set_metadata = None
        self.exp_dir_name = ''

    def train_pseudo(self, data, training_params):
        # process_data ignores self.train_set_metadata if it's None and computes a new one from the actual data
        # or uses the procided one and does not compute a new one if it is not None
        preproc_data, train_set_metadata = preprocess_data(
            data,
            self.train_set_metadata
        )
        self.train_set_metadata = train_set_metadata

        # this is done only if the model is not loaded
        if not self.model:
            update_model_definition_with_metadata(
                self.model_definition,
                train_set_metadata
            )
            self.model = ECD(self.model_definition)

        trainer = Trainer(training_params)
        training_stats = trainer.train(self.model, preproc_data)
        return training_stats

    def train(
            self,
            data_df=None,
            data_train_df=None,
            data_validation_df=None,
            data_test_df=None,
            data_csv=None,
            data_train_csv=None,
            data_validation_csv=None,
            data_test_csv=None,
            data_hdf5=None,
            data_train_hdf5=None,
            data_validation_hdf5=None,
            data_test_hdf5=None,
            data_dict=None,
            data_train_dict=None,
            data_validation_dict=None,
            data_test_dict=None,
            train_set_metadata_json=None,
            experiment_name='api_experiment',
            model_name='run',
            model_resume_path=None,
            skip_save_training_description=False,
            skip_save_training_statistics=False,
            skip_save_model=False,
            skip_save_progress=False,
            skip_save_log=False,
            skip_save_processed_input=False,
            output_directory='results',
            random_seed=42,
            debug=False,
            **kwargs
    ):
        """This function is used to perform a full training of the model on the
           specified dataset.

        # Inputs

        :param data_df: (DataFrame) dataframe containing data. If it has a split
               column, it will be used for splitting (0: train, 1: validation,
               2: test), otherwise the dataset will be randomly split
        :param data_train_df: (DataFrame) dataframe containing training data
        :param data_validation_df: (DataFrame) dataframe containing validation
               data
        :param data_test_df: (DataFrame dataframe containing test data
        :param data_csv: (string) input data CSV file. If it has a split column,
               it will be used for splitting (0: train, 1: validation, 2: test),
               otherwise the dataset will be randomly split
        :param data_train_csv: (string) input train data CSV file
        :param data_validation_csv: (string) input validation data CSV file
        :param data_test_csv: (string) input test data CSV file
        :param data_hdf5: (string) input data HDF5 file. It is an intermediate
               preprocess  version of the input CSV created the first time a CSV
               file is used in the same directory with the same name and a hdf5
               extension
        :param data_train_hdf5: (string) input train data HDF5 file. It is an
               intermediate preprocess  version of the input CSV created the
               first time a CSV file is used in the same directory with the same
               name and a hdf5 extension
        :param data_validation_hdf5: (string) input validation data HDF5 file.
               It is an intermediate preprocess version of the input CSV created
               the first time a CSV file is used in the same directory with the
               same name and a hdf5 extension
        :param data_test_hdf5: (string) input test data HDF5 file. It is an
               intermediate preprocess  version of the input CSV created the
               first time a CSV file is used in the same directory with the same
               name and a hdf5 extension
        :param data_dict: (dict) input data dictionary. It is expected to
               contain one key for each field and the values have to be lists of
               the same length. Each index in the lists corresponds to one
               datapoint. For example a data set consisting of two datapoints
               with a text and a class may be provided as the following dict
               `{'text_field_name': ['text of the first datapoint', text of the
               second datapoint'], 'class_filed_name': ['class_datapoints_1',
               'class_datapoints_2']}`.
        :param data_train_dict: (dict) input training data dictionary. It is
               expected to contain one key for each field and the values have
               to be lists of the same length. Each index in the lists
               corresponds to one datapoint. For example a data set consisting
               of two datapoints with a text and a class may be provided as the
               following dict:
               `{'text_field_name': ['text of the first datapoint', 'text of the
               second datapoint'], 'class_field_name': ['class_datapoint_1',
               'class_datapoint_2']}`.
        :param data_validation_dict: (dict) input validation data dictionary. It
               is expected to contain one key for each field and the values have
               to be lists of the same length. Each index in the lists
               corresponds to one datapoint. For example a data set consisting
               of two datapoints with a text and a class may be provided as the
               following dict:
               `{'text_field_name': ['text of the first datapoint', 'text of the
               second datapoint'], 'class_field_name': ['class_datapoint_1',
               'class_datapoint_2']}`.
        :param data_test_dict: (dict) input test data dictionary. It is
               expected to contain one key for each field and the values have
               to be lists of the same length. Each index in the lists
               corresponds to one datapoint. For example a data set consisting
               of two datapoints with a text and a class may be provided as the
               following dict:
               `{'text_field_name': ['text of the first datapoint', 'text of the
               second datapoint'], 'class_field_name': ['class_datapoint_1',
               'class_datapoint_2']}`.
        :param train_set_metadata_json: (string) input metadata JSON file. It is an
               intermediate preprocess file containing the mappings of the input
               CSV created the first time a CSV file is used in the same
               directory with the same name and a json extension
        :param experiment_name: (string) a name for the experiment, used for the save
               directory
        :param model_name: (string) a name for the model, used for the save
               directory
        :param model_load_path: (string) path of a pretrained model to load as
               initialization
        :param model_resume_path: (string) path of a the model directory to
               resume training of
        :param skip_save_training_description: (bool, default: `False`) disables
               saving the description JSON file.
        :param skip_save_training_statistics: (bool, default: `False`) disables
               saving training statistics JSON file.
        :param skip_save_model: (bool, default: `False`) disables
               saving model weights and hyperparameters each time the model
               improves. By default Ludwig saves model weights after each epoch
               the validation metric imrpvoes, but if the model is really big
               that can be time consuming if you do not want to keep
               the weights and just find out what performance can a model get
               with a set of hyperparameters, use this parameter to skip it,
               but the model will not be loadable later on.
        :param skip_save_progress: (bool, default: `False`) disables saving
               progress each epoch. By default Ludwig saves weights and stats
               after each epoch for enabling resuming of training, but if
               the model is really big that can be time consuming and will uses
               twice as much space, use this parameter to skip it, but training
               cannot be resumed later on.
        :param skip_save_log: (bool, default: `False`) disables saving TensorBoard
               logs. By default Ludwig saves logs for the TensorBoard, but if it
               is not needed turning it off can slightly increase the
               overall speed.
        :param skip_save_processed_input: (bool, default: `False`) skips saving
               intermediate HDF5 and JSON files
        :param output_directory: (string, default: `'results'`) directory that
               contains the results
        :param gpus: (string, default: `None`) list of GPUs to use (it uses the
               same syntax of CUDA_VISIBLE_DEVICES)
        :param gpu_memory_limit: (int: default: `None`) maximum memory in MB to allocate
              per GPU device.
        :param allow_parallel_threads: (bool, default: `True`) allow TensorFlow to use
               multithreading parallelism to improve performance at the cost of
               determinism.
        :param random_seed: (int, default`42`) a random seed that is going to be
               used anywhere there is a call to a random number generator: data
               splitting, parameter initialization and training set shuffling
        :param debug: (bool, default: `False`) enables debugging mode

        There are three ways to provide data: by dataframes using the `_df`
        parameters, by CSV using the `_csv` parameters and by HDF5 and JSON,
        using `_hdf5` and `_json` parameters.
        The DataFrame approach uses data previously obtained and put in a
        dataframe, the CSV approach loads data from a CSV file, while HDF5 and
        JSON load previously preprocessed HDF5 and JSON files (they are saved in
        the same directory of the CSV they are obtained from).
        For all three approaches either a full dataset can be provided (which
        will be split randomly according to the split probabilities defined in
        the model definition, by default 70% training, 10% validation and 20%
        test) or, if it contanins a plit column, it will be plit according to
        that column (interpreting 0 as training, 1 as validation and 2 as test).
        Alternatively separated dataframes / CSV / HDF5 files can beprovided
        for each split.

        During training the model and statistics will be saved in a directory
        `[output_dir]/[experiment_name]_[model_name]_n` where all variables are
        resolved to user spiecified ones and `n` is an increasing number
        starting from 0 used to differentiate different runs.


        # Return

        :return: (dict) a dictionary containing training statistics for each
        output feature containing loss and metrics values for each epoch.
        """
        if data_df is None and data_dict is not None:
            data_df = pd.DataFrame(data_dict)

        if data_train_df is None and data_train_dict is not None:
            data_train_df = pd.DataFrame(data_train_dict)

        if data_validation_df is None and data_validation_dict is not None:
            data_validation_df = pd.DataFrame(data_validation_dict)

        if data_test_df is None and data_test_dict is not None:
            data_test_df = pd.DataFrame(data_test_dict)

        set_on_master(use_horovod)

        # setup directories and file names
        experiment_dir_name = None
        if model_resume_path is not None:
            if os.path.exists(model_resume_path):
                experiment_dir_name = model_resume_path
            else:
                if is_on_master():
                    logger.info(
                        'Model resume path does not exists, '
                        'starting training from scratch'
                    )
                model_resume_path = None

        if model_resume_path is None:
            if is_on_master():
                experiment_dir_name = get_experiment_dir_name(
                    output_directory,
                    experiment_name,
                    model_name
                )
            else:
                experiment_dir_name = None

        # if we are skipping all saving,
        # there is no need to create a directory that will remain empty
        should_create_exp_dir = not (
                skip_save_training_description and
                skip_save_training_statistics and
                skip_save_model and
                skip_save_progress and
                skip_save_log and
                skip_save_processed_input
        )

        description_fn = training_stats_fn = model_dir = None
        if is_on_master():
            if should_create_exp_dir:
                if not os.path.exists(experiment_dir_name):
                    os.makedirs(experiment_dir_name, exist_ok=True)
            description_fn, training_stats_fn, model_dir = get_file_names(
                experiment_dir_name)

        # save description
        if is_on_master():
            description = get_experiment_description(
                self.model_definition,
                data_csv=data_csv,
                data_train_csv=data_train_csv,
                data_validation_csv=data_validation_csv,
                data_test_csv=data_test_csv,
                data_hdf5=data_hdf5,
                data_train_hdf5=data_train_hdf5,
                data_validation_hdf5=data_validation_hdf5,
                data_test_hdf5=data_test_hdf5,
                metadata_json=train_set_metadata_json,
                random_seed=random_seed
            )
            if not skip_save_training_description:
                save_json(description_fn, description)
            # print description
            logger.info('Experiment name: {}'.format(experiment_name))
            logger.info('Model name: {}'.format(model_name))
            logger.info('Output path: {}'.format(experiment_dir_name))
            logger.info('\n')
            for key, value in description.items():
                logger.info('{}: {}'.format(key, pformat(value, indent=4)))
            logger.info('\n')

        # preprocess
        # todo refactoring: make this work with a provided train_set_metadata dict
        preprocessed_data = preprocess_for_training(
            self.model_definition,
            data_df=data_df,
            data_train_df=data_train_df,
            data_validation_df=data_validation_df,
            data_test_df=data_test_df,
            data_csv=data_csv,
            data_train_csv=data_train_csv,
            data_validation_csv=data_validation_csv,
            data_test_csv=data_test_csv,
            data_hdf5=data_hdf5,
            data_train_hdf5=data_train_hdf5,
            data_validation_hdf5=data_validation_hdf5,
            data_test_hdf5=data_test_hdf5,
            train_set_metadata_json=train_set_metadata_json,
            skip_save_processed_input=skip_save_processed_input,
            preprocessing_params=self.model_definition['preprocessing'],
            random_seed=random_seed
        )

        (training_set,
         validation_set,
         test_set,
         train_set_metadata) = preprocessed_data

        if is_on_master():
            logger.info('Training set: {0}'.format(training_set.size))
            if validation_set is not None:
                logger.info('Validation set: {0}'.format(validation_set.size))
            if test_set is not None:
                logger.info('Test set: {0}'.format(test_set.size))

        if is_on_master():
            if not skip_save_model:
                # save train set metadata
                os.makedirs(model_dir, exist_ok=True)
                save_json(
                    os.path.join(
                        model_dir,
                        TRAIN_SET_METADATA_FILE_NAME
                    ),
                    train_set_metadata
                )

        contrib_command("train_init", experiment_directory=experiment_dir_name,
                        experiment_name=experiment_name, model_name=model_name,
                        output_directory=output_directory,
                        resume=model_resume_path is not None)

        # Build model if not provided
        # if it was provided it means it was already loaded
        if not self.model:
            if is_on_master():
                print_boxed('MODEL', print_fun=logger.debug)
            # update model definition with metadata properties
            update_model_definition_with_metadata(
                self.model_definition,
                train_set_metadata
            )
            self.model = ECD(
                input_features_def=self.model_definition['input_features'],
                combiner_def=self.model_definition['combiner'],
                output_features_def=self.model_definition['output_features'],
            )

        # init trainer
        trainer = Trainer(
            **self.model_definition[TRAINING],
            debug=debug
        )

        contrib_command("train_model", self.model, self.model_definition,
                        self.model_load_path)

        # train model
        if is_on_master():
            print_boxed('TRAINING')
        train_stats = trainer.train(
            self.model,
            training_set,
            validation_set=validation_set,
            test_set=test_set,
            save_path=model_dir,
        )

        train_trainset_stats, train_valiset_stats, train_testset_stats = train_stats
        train_stats = {
            TRAINING: train_trainset_stats,
            VALIDATION: train_valiset_stats,
            TEST: train_testset_stats
        }

        # save training statistics
        if is_on_master():
            if not skip_save_training_statistics:
                save_json(training_stats_fn, train_stats)

        # grab the results of the model with highest validation test performance
        validation_field = self.model_definition[TRAINING]['validation_field']
        validation_metric = self.model_definition[TRAINING][
            'validation_metric']
        validation_field_result = train_valiset_stats[validation_field]

        best_function = get_best_function(validation_metric)
        # results of the model with highest validation test performance
        if is_on_master() and validation_set is not None:
            epoch_best_vali_metric, best_vali_metric = best_function(
                enumerate(validation_field_result[validation_metric]),
                key=lambda pair: pair[1]
            )
            logger.info(
                'Best validation model epoch: {0}'.format(
                    epoch_best_vali_metric + 1)
            )
            logger.info(
                'Best validation model {0} on validation set {1}: {2}'.format(
                    validation_metric, validation_field, best_vali_metric
                ))
            if test_set is not None:
                best_vali_metric_epoch_test_metric = train_testset_stats[
                    validation_field][validation_metric][
                    epoch_best_vali_metric]

                logger.info(
                    'Best validation model {0} on test set {1}: {2}'.format(
                        validation_metric,
                        validation_field,
                        best_vali_metric_epoch_test_metric
                    )
                )
            logger.info(
                '\nFinished: {0}_{1}'.format(experiment_name, model_name))
            logger.info('Saved to: {0}'.format(experiment_dir_name))

        contrib_command("train_save", experiment_dir_name)

        self.train_set_metadata = preprocessed_data[-1]

        return train_stats

    def predict(self, data):
        preproc_data = preprocess_data(data)
        preds = self.model.batch_predict(preproc_data)
        postproc_preds = postprocess_data(preds)
        return postproc_preds

    def evaluate(self, data, return_preds=False):
        preproc_data = preprocess_data(data)
        if return_preds:
            eval_stats, preds = self.model.batch_evaluate(
                preproc_data, return_preds=return_preds
            )
            postproc_preds = postprocess_data(preds)
            return eval_stats, postproc_preds
        else:
            eval_stats = self.model.batch_evaluate(
                preproc_data, return_preds=return_preds
            )
            return eval_stats

    @staticmethod
    def load(model_dir,
             logging_level=logging.ERROR,
             use_horovod=False,
             gpus=None,
             gpu_memory_limit=None,
             allow_parallel_threads=True):
        """This function allows for loading pretrained models


        # Inputs

        :param model_dir: (string) path to the directory containing the model.
               If the model was trained by the `train` or `experiment` command,
               the model is in `results_dir/experiment_dir/model`.
        :param gpus: (string, default: `None`) list of GPUs to use (it uses the
               same syntax of CUDA_VISIBLE_DEVICES)
        :param gpu_memory_limit: (int: default: `None`) maximum memory in MB to allocate
              per GPU device.
        :param allow_parallel_threads: (bool, default: `True`) allow TensorFlow to use
               multithreading parallelism to improve performance at the cost of
               determinism.

        # Return

        :return: (LudwigModel) a LudwigModel object


        # Example usage

        ```python
        ludwig_model = LudwigModel.load(model_dir)
        ```

        """
        # load model definition
        model_definition = load_json(
            os.path.join(
                model_dir,
                MODEL_HYPERPARAMETERS_FILE_NAME
            )
        )

        # initialize model
        ludwig_model = NewLudwigModel(
            model_definition,
            logging_level=logging_level,
            use_horovod=use_horovod,
            gpus=gpus,
            gpu_memory_limit=gpu_memory_limit,
            allow_parallel_threads=allow_parallel_threads,
        )

        # load model weights
        weights_save_path = os.path.join(
            model_dir,
            MODEL_WEIGHTS_FILE_NAME
        )
        ludwig_model.model.load_weights(weights_save_path)

        # load train set metadata
        ludwig_model.train_set_metadata = load_metadata(
            os.path.join(
                model_dir,
                TRAIN_SET_METADATA_FILE_NAME
            )
        )

        return ludwig_model

    def save(self, save_path):
        """This function allows to save models on disk

        # Inputs

        :param  save_path: (string) path to the directory where the model is
                going to be saved. Both a JSON file containing the model
                architecture hyperparameters and checkpoints files containing
                model weights will be saved.


        # Example usage

        ```python
        ludwig_model.save(save_path)
        ```

        """
        if (self.model is None
                or self.model_definition is None
                or self.train_set_metadata is None):
            raise ValueError('Model has not been initialized or loaded')

        # save model definition
        model_hyperparameters_path = os.path.join(
            save_path,
            MODEL_HYPERPARAMETERS_FILE_NAME
        )
        self.model.save_definition(
            model_hyperparameters_path
        )

        # save model weights
        model_weights_path = os.path.join(save_path, MODEL_WEIGHTS_FILE_NAME)
        self.model.model.save_weights(model_weights_path)

        # save training set metadata
        train_set_metadata_path = os.path.join(
            save_path,
            TRAIN_SET_METADATA_FILE_NAME
        )
        save_json(train_set_metadata_path, self.train_set_metadata)

    def save_for_serving(self, save_path):
        """This function allows to save models on disk

        # Inputs

        :param  save_path: (string) path to the directory where the SavedModel
                is going to be saved.


        # Example usage

        ```python
        ludwig_model.save_for_serving(save_path)
        ```

        """
        if (self.model is None or self.model._session is None or
                self.model_definition is None or self.train_set_metadata is None):
            raise ValueError('Model has not been initialized or loaded')

        self.model.save_savedmodel(save_path)

    @staticmethod
    def set_logging_level(logging_level):
        """
        :param logging_level: Set/Update the logging level. Use logging
        constants like `logging.DEBUG` , `logging.INFO` and `logging.ERROR`.

        :return: None
        """
        logging.getLogger('ludwig').setLevel(logging_level)
        if logging_level in {logging.WARNING, logging.ERROR, logging.CRITICAL}:
            set_disable_progressbar(True)
        else:
            set_disable_progressbar(False)
