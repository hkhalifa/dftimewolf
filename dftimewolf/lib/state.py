"""This class maintains the internal dfTimewolf state.

Use it to track errors, abort on global failures, cleanup after modules, etc.
"""
# TODO(tomchop): Make sure docstrings here follow the same type hinting as the
# rest of the codebase
from __future__ import print_function
from __future__ import unicode_literals

import sys
import threading

from dftimewolf.lib import utils
from dftimewolf.lib.errors import DFTimewolfError


class DFTimewolfState(object):
  """The main State class.

  Attributes:
    errors: [(str, bool)] The errors generated by a module. These should be
        cleaned up after each module run using the cleanup() method.
    global_errors: [(str, bool)] the cleanup() method moves non critical errors
        to this attribute for later reporting.
    input: list, The data that the current module will use as input.
    output: list, The data that the current module generates.
    store: dict, store of arbitrary data for modules.
  """

  def __init__(self, config):
    self.errors = []
    self.global_errors = []
    self.input = []
    self.output = []
    self.store = {}
    self._store_lock = threading.Lock()
    self._module_pool = {}
    self.config = config
    self.recipe = None
    self.events = {}

  def load_recipe(self, recipe):
    """Populates the internal module pool with modules declared in a recipe.

    Args:
      recipe: Dict, recipe declaring modules to load.
    """
    self.recipe = recipe
    for module_description in recipe['modules']:
      # Combine CLI args with args from the recipe description
      module_name = module_description['name']
      module = self.config.get_module(module_name)(self)
      self._module_pool[module_name] = module

  def store_container(self, container):
    """Thread-safe method to store data in the state's store.

    Args:
      container (containers.interface.AttributeContainer): The data to store.
    """
    with self._store_lock:
      self.store.setdefault(container.CONTAINER_TYPE, []).append(container)

  def get_containers(self, container_class):
    """Thread-safe method to retrieve data from the state's store.

    Args:
      container_class: AttributeContainer class used to filter data.

    Returns:
      A list of AttributeContainer objects of matching CONTAINER_TYPE.
    """
    with self._store_lock:
      return self.store.get(container_class.CONTAINER_TYPE, [])

  def setup_modules(self, args):
    """Performs setup tasks for each module in the module pool.

    Threads declared modules' setup() functions. Takes CLI arguments into
    account when replacing recipe parameters for each module.

    Args:
      args: Command line arguments that will be used to replace the parameters
          declared in the recipe.
    """

    def _setup_module_thread(module_description):
      """Calls the module's setup() function and sets an Event object for it.

      Args:
        module_description (dict): Corresponding recipe module description.
      """
      new_args = utils.import_args_from_dict(
          module_description['args'], vars(args), self.config)
      module = self._module_pool[module_description['name']]
      try:
        module.setup(**new_args)
      except Exception as error:  # pylint: disable=broad-except
        self.add_error(
            'An unknown error occurred: {0!s}'.format(error), critical=True)
      self.events[module_description['name']] = threading.Event()
      self.cleanup()

    threads = []
    for module_description in self.recipe['modules']:
      t = threading.Thread(
          target=_setup_module_thread,
          args=(module_description, )
      )
      threads.append(t)
      t.start()
    for t in threads:
      t.join()

    self.check_errors(is_global=True)

  def run_modules(self):
    """Performs the actual processing for each module in the module pool."""

    def _run_module_thread(module_description):
      """Runs the module's process() function.

      Waits for any blockers to have finished before running process(), then
      sets an Event flag declaring the module has completed.
      """
      for blocker in module_description['wants']:
        self.events[blocker].wait()
      module = self._module_pool[module_description['name']]
      try:
        module.process()
      except DFTimewolfError as error:
        self.add_error(error.message, critical=True)
      except Exception as error:  # pylint: disable=broad-except
        self.add_error(
            'An unknown error occurred: {0!s}'.format(error), critical=True)
      print('Module {0:s} completed'.format(module_description['name']))
      self.events[module_description['name']].set()
      self.cleanup()

    threads = []
    for module_description in self.recipe['modules']:
      t = threading.Thread(
          target=_run_module_thread,
          args=(module_description, )
      )
      threads.append(t)
      t.start()
    for t in threads:
      t.join()

    self.check_errors(is_global=True)

  def add_error(self, error, critical=False):
    """Adds an error to the state.

    Args:
      error: The text that will be added to the error list.
      critical: If set to True and the error is checked with check_errors, will
          dfTimewolf will abort.
    """
    self.errors.append((error, critical))

  def cleanup(self):
    """Basic cleanup after modules.

    The state's output becomes the input for the next stage. Any errors are
    moved to the global_errors attribute so that they can be reported at a
    later stage.
    """
    # Move any existing errors to global errors
    self.global_errors.extend(self.errors)
    self.errors = []

    # Make the previous module's output available to the next module
    self.input = self.output
    self.output = []

  def check_errors(self, is_global=False):
    """Checks for errors and exits if any of them are critical.

    Args:
      is_global: If True, check the global_errors attribute. If false, check the
          error attribute.
    """
    errors = self.global_errors if is_global else self.errors
    if errors:
      print('dfTimewolf encountered one or more errors:')
      for error, critical in errors:
        print('{0:s}  {1:s}'.format('CRITICAL: ' if critical else '', error))
        if critical:
          print('Critical error found. Aborting.')
          sys.exit(-1)
