import numpy as np

from bathygrid.convenience import create_grid
from HSTB.kluster.kluster_variables import default_roll_error, default_pitch_error, default_heading_error, \
    default_x_offset_error, default_y_offset_error


class PatchTest:
    """
    Patch test utility based on "Computation of Calibration Parameters for Multibeam Echo Sounders Using the
    Least Squares Method", by Jan Terje Bjorke.  Compute new offsets/angles for the data provided using this automated
    least squares adjustment.  Data should be provided only for a pair of lines, lines that are reciprocal and overlap as
    much as possible.  We expect the input to be a FQPR subset generated by Kluster.  We take the non-rejected points,
    build a Bathygrid gridded surface, construct the required matrices and store the result for each line (self.result).
    The result should be of shape (6,2), with six patch test parameters available per line.

    The six parameters are roll, pitch, heading, x translation, y translation and horizontal scale factor.
    """

    def __init__(self, fqpr, azimuth: float = None, initial_parameters: dict = None):
        """
        Parameters
        ----------
        fqpr
            'Fully processed ping record', Kluster processed FQPR instance subset to just the two lines of interest
        azimuth
            azimuth of one of the lines that we will use to rotate all points
        initial_parameters
            either a dict of the initial parameters or None, which will use zeros for the start values and default
            standard uncertainties
        """

        self.fqpr = fqpr
        self.azimuth = azimuth
        self._default_parameters = {'roll': 0.0, 'roll_unc': default_roll_error, 'pitch': 0.0, 'pitch_unc': default_pitch_error,
                                    'heading': 0.0, 'heading_unc': default_heading_error, 'x_unc': default_x_offset_error,
                                    'y_unc': default_y_offset_error, 'h_scale_unc': 0.01}
        if initial_parameters is None:
            self.initial_parameters = self._default_parameters
        else:
            missing_keys = [x for x in self._default_parameters.keys() if x not in initial_parameters.keys()]
            if missing_keys:
                raise ValueError('The following keys are not in the provided parameters dict, they must be included: {}'.format(missing_keys))
            self.initial_parameters = initial_parameters

        self.multibeam_files = None
        self.multibeam_indexes = None
        self.points = None
        self.min_x = None
        self.min_y = None
        self.grid = None
        self.a_matrix = None
        self.b_matrix = None
        self.result = None

    def run_patch(self):
        """
        Run the patch test procedure, saving the adjustments to the result attribute.
        """

        self._generate_rotated_points()
        self._grid()
        self._build_patch_test_values()
        self._compute_least_squares()

    def display_results(self):
        print('Patch test results')
        if self.fqpr is not None and self.result is not None:
            print('Lines: {}'.format(list(self.fqpr.multibeam.raw_ping[0].multibeam_files.keys())))
            print('roll: {}'.format([x for x in self.result[0]]))
            print('pitch: {}'.format([x for x in self.result[1]]))
            print('heading: {}'.format([x for x in self.result[2]]))
            print('x_translation: {}'.format([x for x in self.result[3]]))
            print('y_translation: {}'.format([x for x in self.result[4]]))
            print('horizontal scale factor: {}'.format([x for x in self.result[5]]))

    def _generate_rotated_points(self):
        """
        Convert the northings/eastings/depths to the model coordinate system.  This coordinate system is defined as:

        x = + Forward, y = + Starboard, z = + Down, Roll = + Port down, Pitch = + Bow down, Yaw = + Counterclockwise

        The test computes the following parameters:

        roll, pitch, heading, x_translation, y_translation, horizontal scale factor

        First, we pull the valid soundings from the Fqpr instance, using the filter_by_detection option to remove
        rejected soundings.  The points are returned for each line, which we then rotate according to one of the line
        azimuths to get an eastern orientation, where +X would be forward for the sonar.  Then, we flip the y values
        to get positive to starboard and normalize both northings and eastings to get xy values in the new model coordinate
        system.

        The result is stored in the self.points attribute as a new structured numpy array.
        """

        ang = self.azimuth - 90  # rotations are counter clockwise, we want it eventually facing east
        cos_az = np.cos(np.deg2rad(ang))
        sin_az = np.sin(np.deg2rad(ang))
        finalx = None
        finaly = None
        finalz = None
        self.multibeam_files = self.fqpr.multibeam.raw_ping[0].multibeam_files
        self.multibeam_indexes = {}
        curr_point_index = 0
        for mfilename in self.multibeam_files.keys():
            data = self.fqpr.subset_variables_by_line(['x', 'y', 'z'], mfilename, filter_by_detection=True)[mfilename]
            x, y, z = data.x.values, data.y.values, data.z.values
            self.multibeam_indexes[mfilename] = [curr_point_index, curr_point_index + x.size]
            curr_point_index = x.size
            if finalx is None:
                finalx = x
                finaly = y
                finalz = z
            else:
                finalx = np.concatenate([finalx, x])
                finaly = np.concatenate([finaly, y])
                finalz = np.concatenate([finalz, z])
        if finalx.any():
            dtyp = [('x', np.float64), ('y', np.float64), ('z', np.float32)]
            self.points = np.empty(len(finalx), dtype=dtyp)
            self.points['x'] = finalx
            self.points['y'] = finaly
            self.points['z'] = finalz

            # # normalize the y axis
            # self.points['y'] = self.points['y'] - self.points['y'].min()
            # # normalize the x axis
            # self.points['x'] = self.points['x'] - self.points['x'].min()

            # calculate center of rotation
            self.min_x = self.points['x'].min()
            self.min_y = self.points['y'].min()
            centered_x = self.points['x'] - self.min_x
            centered_y = self.points['y'] - self.min_y

            # rotate according to the provided line azimuth
            self.points['x'] = self.min_x + cos_az * centered_x - sin_az * centered_y
            self.points['y'] = self.min_y + sin_az * centered_x + cos_az * centered_y

        else:
            print('Found no valid points for {}'.format(list(self.multibeam_files.keys())))

    def _grid(self):
        """
        Compute an in memory bathygrid grid, single resolution with depth automatically determined by the depth of the
        tiles, using the bathygrid depth lookup table.  We add points by line so that we can use the line name to return
        the gridded depth values for each line later.
        """

        if self.points is not None and self.points.size > 0:
            grid_class = create_grid(grid_type='single_resolution')
            for linename in self.multibeam_indexes:
                idxs = self.multibeam_indexes[linename]
                grid_class.add_points(self.points[idxs[0]:idxs[1]], linename, [linename], progress_bar=False)
            grid_class.grid(progress_bar=False)
            self.grid = grid_class

    def _build_patch_test_values(self):
        """
        Build the a and b matrices for the least squares calculation.  The equation used is:

        Parameters = (A_transpose * p_one * A + p_two) ( x ) = (A_transpose * p_one * l_one)

        Which leaves us wth self.a_matrix = A_transpose * p_one * A + p_two and self.b_matrix = A_transpose * p_one * l_one
        """

        if self.grid is not None:
            line_layers = list(self.multibeam_indexes.keys())
            dpth, xslope, yslope, lineone, linetwo = self.grid.get_layers_by_name(['depth', 'x_slope', 'y_slope', line_layers[0], line_layers[1]])
            valid_index = np.logical_and(~np.isnan(lineone), ~np.isnan(linetwo))
            if valid_index.any():
                xval = np.arange(self.grid.min_x, self.grid.max_x, self.grid.resolutions[0])
                yval = np.arange(self.grid.min_y, self.grid.max_y, self.grid.resolutions[0])
                grid_rez = self.grid.resolutions[0]
                # compute the x and y node locations for each grid node in the grid
                x_node_locs, y_node_locs = np.meshgrid(xval + grid_rez / 2, yval + grid_rez / 2, copy=False)

                dpth_valid = dpth[valid_index]  # grid depth for all grid nodes where both lines overlap
                y_node_valid = y_node_locs[valid_index]  # y coordinate for all grid nodes where both lines overlap
                xslope_valid = xslope[valid_index]  # grid slope partial x for all grid nodes where both lines overlap
                yslope_valid = yslope[valid_index]  # grid slope partial y for all grid nodes where both lines overlap
                lineone_valid = lineone[valid_index]  # grid depth for line one for all grid nodes where both lines overlap
                linetwo_valid = linetwo[valid_index]  # grid slope partial x for all grid nodes where both lines overlap

                # A-matrix is in order of roll, pitch, heading, x_translation, y_translation, horizontal scale factor
                a_matrix = np.column_stack([yslope_valid * dpth_valid - y_node_valid,
                                            xslope_valid * dpth_valid,
                                            xslope_valid * y_node_valid,
                                            xslope_valid,
                                            yslope_valid,
                                            yslope_valid * y_node_valid])
                l_one_matrix = np.column_stack([lineone_valid, linetwo_valid])
                # p_one can contain 1/grid node uncertainty in the future, currently we leave it out
                # p_one_matrix = np.identity(self.a_matrix.shape[0])
                # p_two_matrix = np.identity(6) * [1 / np.deg2rad(self.initial_parameters['roll_unc']), 1 / np.deg2rad(self.initial_parameters['pitch_unc']),
                #                                  1 / np.deg2rad(self.initial_parameters['heading_unc']) ** 2, 1 / self.initial_parameters['x_unc'],
                #                                  1 / self.initial_parameters['y_unc'] ** 2, 1 / self.initial_parameters['h_scale_unc']]
                a_t = a_matrix.T
                # self.a_matrix = np.dot(a_t, a_matrix) + p_two_matrix
                self.a_matrix = np.dot(a_t, a_matrix)
                self.b_matrix = np.dot(a_t, l_one_matrix)
            else:
                print('No valid overlap found for lines: {}'.format(list(self.multibeam_files.keys())))

    def _compute_least_squares(self):
        if self.a_matrix is not None and self.b_matrix is not None:
            self.result, residuals, rank, singular = np.linalg.lstsq(self.a_matrix, self.b_matrix, rcond=None)
