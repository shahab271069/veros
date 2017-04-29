import math
import warnings
import logging

import numpy
if numpy.__name__ == "bohrium":
    warnings.warn(
        "Running veros with 'python -m bohrium' is discouraged (use '--backend bohrium' instead)")
    import numpy_force
    numpy = numpy_force
try:
    import bohrium
    import bohrium.lapack
except ImportError:
    warnings.warn("Could not import Bohrium")
    bohrium = None

BACKENDS = {"numpy": numpy, "bohrium": bohrium}

# for some reason, netCDF4 has to be imported before h5py, so we do it here
import netCDF4
import h5py

from . import variables, settings, cli, diagnostics, time, handlers
from .timer import Timer
from .core import momentum, numerics, thermodynamics, eke, tke, idemix, \
    isoneutral, external, advection, cyclic


class Veros(object):
    """Main class for Veros, used for building a model and running it.

    Note:
        This class is meant to be subclassed. Subclasses need to implement the
        methods :meth:`set_parameter`, :meth:`set_topography`, :meth:`set_grid`,
        :meth:`set_coriolis`, :meth:`set_initial_conditions`, :meth:`set_forcing`,
        and :meth:`set_diagnostics`.

    Args:
        backend (:obj:`bool`, optional): Backend to use for array operations.
            Possible values are ``numpy`` and ``bohrium``. Defaults to ``None``, which
            tries to read the backend from the command line (set via a flag
            ``-b``/``--backend``), and uses ``numpy`` if no command line argument is given.
        loglevel (one of {debug, info, warning, error, critical}, optional): Verbosity
            of the model. Tries to read value from command line if not given
            (``-v``/``--loglevel``). Defaults to ``info``.
        logfile (path, optional): Path to a log file to write output to. Tries to
            read value from command line if not given (``-l``/``--logfile``). Defaults
            to stdout.

    Example:
        >>> import matplotlib.pyplot as plt
        >>> from climate.veros import Veros
        >>>
        >>> class MyModel(Veros):
        >>>     ...
        >>>
        >>> simulation = MyModel(backend="bohrium")
        >>> simulation.run()
        >>> plt.imshow(simulation.psi)
        >>> plt.show()
    """

    # Constants
    pi = numpy.pi
    radius = 6370.0e3  # Earth radius in m
    degtom = radius / 180.0 * pi  # Conversion degrees latitude to meters
    mtodeg = 1 / degtom  # Conversion meters to degrees latitude
    omega = pi / 43082.0  # Earth rotation frequency in 1/s
    rho_0 = 1024.0  # Boussinesq reference density in :math:`kg/m^3`
    grav = 9.81  # Gravitational constant in :math:`m/s^2`

    def __init__(self, backend=None, loglevel=None, logfile=None):
        args = cli.parse_command_line()
        self.backend, self.backend_name = self._get_backend(backend or args.backend)
        try: # python 2
            logging.basicConfig(logfile=logfile or args.logfile, filemode="w",
                                level=getattr(logging, (loglevel or args.loglevel).upper()),
                                format="%(message)s")
        except ValueError: # python 3
            logging.basicConfig(filename=logfile or args.logfile, filemode="w",
                                level=getattr(logging, (loglevel or args.loglevel).upper()),
                                format="%(message)s")
        self.profile_mode = args.profile
        self._set_default_settings()
        self._command_line_settings = args.set or {}
        self.timers = {k: Timer(k) for k in ("setup", "main", "momentum", "temperature",
                                             "eke", "idemix", "tke", "diagnostics",
                                             "pressure", "friction", "isoneutral",
                                             "vmix", "eq_of_state")}

    def _get_backend(self, backend):
        if backend not in BACKENDS.keys():
            raise ValueError("unrecognized backend {} (must be either of: {!r})".format(
                backend, BACKENDS.keys()))
        if BACKENDS[backend] is None:
            raise ValueError("{} backend failed to import".format(backend))
        return BACKENDS[backend], backend

    def _set_default_settings(self):
        for key, setting in settings.SETTINGS.items():
            setattr(self, key, setting.type(setting.default))

    def _set_commandline_settings(self):
        for key, val in self._command_line_settings:
            setattr(self, key, settings.SETTINGS[key].type(val))

    def _allocate(self):
        self.nisle = 0 # to be overriden during streamfunction_init

        self.variables = {}

        def init_var(var_name, var):
            shape = variables.get_dimensions(self, var.dims)
            setattr(self, var_name, self.backend.zeros(shape, dtype=var.dtype))
            self.variables[var_name] = var

        for var_name, var in variables.MAIN_VARIABLES.items():
            init_var(var_name, var)
        for condition, var_dict in variables.CONDITIONAL_VARIABLES.items():
            if condition.startswith("not "):
                eval_condition = not bool(getattr(self, condition[4:]))
            else:
                eval_condition = bool(getattr(self, condition))
            if eval_condition:
                for var_name, var in var_dict.items():
                    init_var(var_name, var)

    def _not_implemented(self):
        raise NotImplementedError("Needs to be implemented by subclass")

    def set_parameter(self):
        """To be implemented by subclass.

        First function to be called during setup.
        Use this to modify the model settings.

        Example:
          >>> def set_parameter(self):
          >>>     self.nx, self.ny, self.nz = (360, 120, 50)
          >>>     self.coord_degree = True
          >>>     self.enable_cyclic = True
        """
        self._not_implemented()

    def set_initial_conditions(self):
        """To be implemented by subclass.

        May be used to set initial conditions.

        Example:
          >>> @veros_method
          >>> def set_initial_conditions(self):
          >>>     self.u[:, :, :, self.tau] = np.random.rand(self.u.shape[:-1])
        """
        self._not_implemented()

    def set_grid(self):
        """To be implemented by subclass.

        Has to set the grid spacings :attr:`dxt`, :attr:`dyt`, and :attr:`dzt`,
        along with the coordinates of the grid origin, :attr:`x_origin` and
        :attr:`y_origin`.

        Example:
          >>> @veros_method
          >>> def set_grid(self):
          >>>     self.x_origin, self.y_origin = 0, 0
          >>>     self.dxt[...] = [0.1, 0.05, 0.025, 0.025, 0.05, 0.1]
          >>>     self.dyt[...] = 1.
          >>>     self.dzt[...] = [10, 10, 20, 50, 100, 200]
        """
        self._not_implemented()

    def set_coriolis(self):
        """To be implemented by subclass.

        Has to set the Coriolis parameter :attr:`coriolis_t` at T grid cells.

        Example:
          >>> @veros_method
          >>> def set_coriolis(self):
          >>>     self.coriolis_t[:, :] = 2 * self.omega * np.sin(self.yt[np.newaxis, :] / 180. * self.pi)
        """
        self._not_implemented()

    def set_topography(self):
        """To be implemented by subclass.

        Must specify the model topography by setting :attr:`kbot`.

        Example:
          >>> @veros_method
          >>> def set_topography(self):
          >>>     self.kbot[:, :] = 10
          >>>     # add a rectangular island somewhere inside the domain
          >>>     self.kbot[10:20, 10:20] = 0
        """
        self._not_implemented()

    def set_forcing(self):
        """To be implemented by subclass.

        Called before every time step to update the external forcing, e.g. through
        :attr:`forc_temp_surface`, :attr:`forc_salt_surface`, :attr:`surface_taux`,
        :attr:`surface_tauy`, :attr:`forc_tke_surface`, :attr:`temp_source`, or
        :attr:`salt_source`. Use this method to implement time-dependent forcing.

        Example:
          >>> @veros_method
          >>> def set_forcing(self):
          >>>     current_month = (self.time / (31 * 24 * 60 * 60)) % 12
          >>>     self.surface_taux[:, :] = self._windstress_data[:, :, current_month]
        """
        self._not_implemented()

    def set_diagnostics(self):
        """To be implemented by subclass.

        Called before setting up the :ref:`diagnostics <diagnostics>`. Use this method e.g. to
        mark additional :ref:`variables <variables>` for output.

        Example:
          >>> @veros_method
          >>> def set_diagnostics(self):
          >>>     self.diagnostics["snapshot"].output_vars += ["drho", "dsalt", "dtemp"]
        """
        self._not_implemented()

    def flush(self):
        """Flush computations if supported by the current backend.
        """
        try:
            self.backend.flush()
        except AttributeError:
            pass

    def sanity_check(self):
        """Checks for solver convergence
        """
        return self.backend.all(self.backend.isfinite(self.u))

    def setup(self):
        with self.timers["setup"]:
            logging.info("Setting up everything")
            
            self.set_parameter()
            self._set_commandline_settings()
            self._allocate()

            self.set_grid()
            numerics.calc_grid(self)

            self.set_coriolis()
            numerics.calc_beta(self)

            self.set_topography()
            numerics.calc_topo(self)

            self.set_initial_conditions()
            numerics.calc_initial_conditions(self)

            self.set_forcing()
            external.streamfunction_init(self)

            logging.info("Initializing diagnostics")
            self.diagnostics = {name: Diagnostic(self)
                                for name, Diagnostic in diagnostics.diagnostics.items()}
            self.set_diagnostics()
            for name, diagnostic in self.diagnostics.items():
                diagnostic.initialize(self)
                if diagnostic.sampling_frequency:
                    logging.info(" running diagnostic '{0}' every {1[0]:.1f} {1[1]} / {2:.1f} time steps"
                                 .format(name, time.format_time(self, diagnostic.sampling_frequency),
                                         diagnostic.sampling_frequency / self.dt_tracer))
                if diagnostic.output_frequency:
                    logging.info(" writing output for diagnostic '{0}' every {1[0]:.1f} {1[1]} / {2:.1f} time steps"
                                 .format(name, time.format_time(self, diagnostic.output_frequency),
                                         diagnostic.output_frequency / self.dt_tracer))

            eke.init_eke(self)

            isoneutral.check_isoneutral_slope_crit(self)

            if self.enable_tke and not self.enable_implicit_vert_friction:
                raise RuntimeError("use TKE model only with implicit vertical friction"
                                   "(set enable_implicit_vert_fricton)")

            if self.restart_input_filename:
                logging.info("Reading restarts:")
                for diagnostic in self.diagnostics.values():
                    diagnostic.read_restart(self)


    def run(self):
        """Main routine of the simulation.
        """
        self.enditt = self.itt + int(self.runlen / self.dt_tracer) - 1
        logging.info("Starting integration for {0[0]:.1f} {0[1]}".format(time.format_time(self, self.runlen)))
        logging.info(" from time step {} to {}".format(self.itt, self.enditt))

        start_iteration = self.itt
        with handlers.signals_to_exception():
            try:
                while self.itt <= self.enditt:
                    logging.info("Current iteration: {}".format(self.itt))

                    with self.timers["diagnostics"]:
                        t = time.current_time(self, "seconds")
                        if self.restart_frequency and t % self.restart_frequency < self.dt_tracer:
                            for diagnostic in self.diagnostics.values():
                                diagnostic.write_restart(self)

                    if self.itt - start_iteration == 3 and self.profile_mode:
                        # when using bohrium, most kernels should be pre-compiled
                        # after three iterations
                        import pyinstrument
                        profiler = pyinstrument.Profiler()
                        profiler.start()

                    with self.timers["main"]:
                        self.set_forcing()

                        if self.enable_idemix:
                            idemix.set_idemix_parameter(self)

                        eke.set_eke_diffusivities(self)
                        tke.set_tke_diffusivities(self)

                        with self.timers["momentum"]:
                            momentum.momentum(self)

                        with self.timers["temperature"]:
                            thermodynamics.thermodynamics(self)

                        if self.enable_eke or self.enable_tke or self.enable_idemix:
                            advection.calculate_velocity_on_wgrid(self)

                        with self.timers["eke"]:
                            if self.enable_eke:
                                eke.integrate_eke(self)

                        with self.timers["idemix"]:
                            if self.enable_idemix:
                                idemix.integrate_idemix(self)

                        with self.timers["tke"]:
                            if self.enable_tke:
                                tke.integrate_tke(self)

                        if self.enable_cyclic_x:
                            cyclic.setcyclic_x(self.u[:, :, :, self.taup1])
                            cyclic.setcyclic_x(self.v[:, :, :, self.taup1])
                            if self.enable_tke:
                                cyclic.setcyclic_x(self.tke[:, :, :, self.taup1])
                            if self.enable_eke:
                                cyclic.setcyclic_x(self.eke[:, :, :, self.taup1])
                            if self.enable_idemix:
                                cyclic.setcyclic_x(self.E_iw[:, :, :, self.taup1])

                        momentum.vertical_velocity(self)

                    self.flush()
                    self.itt += 1

                    with self.timers["diagnostics"]:
                        if not self.sanity_check():
                            raise RuntimeError("solver diverged at iteration {}".format(self.itt))

                        if self.enable_neutral_diffusion and self.enable_skew_diffusion:
                            isoneutral.isoneutral_diag_streamfunction(self)

                        for diagnostic in self.diagnostics.values():
                            if diagnostic.sampling_frequency and t % diagnostic.sampling_frequency < self.dt_tracer:
                                diagnostic.diagnose(self)
                            if diagnostic.output_frequency and t % diagnostic.output_frequency < self.dt_tracer:
                                diagnostic.output(self)

                    logging.debug("Time step took {}s".format(self.timers["main"].getLastTime()))

                    # permutate time indices
                    self.taum1, self.tau, self.taup1 = self.tau, self.taup1, self.taum1


            except:
                logging.critical("stopping integration at iteration {}".format(self.itt))
                raise

            finally:
                for diagnostic in self.diagnostics.values():
                    diagnostic.write_restart(self)

                logging.debug("Timing summary:")
                logging.debug(" setup time               = {:.1f}s"
                              .format(self.timers["setup"].getTime()))
                logging.debug(" main loop time           = {:.1f}s"
                              .format(self.timers["main"].getTime()))
                logging.debug("     momentum             = {:.1f}s"
                              .format(self.timers["momentum"].getTime()))
                logging.debug("       pressure           = {:.1f}s"
                              .format(self.timers["pressure"].getTime()))
                logging.debug("       friction           = {:.1f}s"
                              .format(self.timers["friction"].getTime()))
                logging.debug("     thermodynamics       = {:.1f}s"
                              .format(self.timers["temperature"].getTime()))
                logging.debug("       lateral mixing     = {:.1f}s"
                              .format(self.timers["isoneutral"].getTime()))
                logging.debug("       vertical mixing    = {:.1f}s"
                              .format(self.timers["vmix"].getTime()))
                logging.debug("       equation of state  = {:.1f}s"
                              .format(self.timers["eq_of_state"].getTime()))
                logging.debug("     EKE                  = {:.1f}s"
                              .format(self.timers["eke"].getTime()))
                logging.debug("     IDEMIX               = {:.1f}s"
                              .format(self.timers["idemix"].getTime()))
                logging.debug("     TKE                  = {:.1f}s"
                              .format(self.timers["tke"].getTime()))
                logging.debug(" diagnostics and I/O      = {:.1f}s"
                              .format(self.timers["diagnostics"].getTime()))

                if self.profile_mode:
                    try:
                        profiler.stop()
                        with open("profile.html", "w") as f:
                            f.write(profiler.output_html())
                    except UnboundLocalError:  # profiler has not been started
                        pass

                logging.shutdown()
