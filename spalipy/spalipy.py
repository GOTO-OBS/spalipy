import argparse
import itertools

from astropy.io import fits
from astropy.table import Table
import numpy as np
from scipy import linalg, interpolate
from scipy.ndimage import interpolation, map_coordinates
from scipy.spatial import cKDTree, distance

# hack to expose dfitpack errors so we can catch them later
try:
    interpolate.dfitpack.sproot(-1, -1, -1)
except Exception as e:
    dfitpackError = type(e)

# SExtractor column definitions
X = 'X_IMAGE'
Y = 'Y_IMAGE'
FLUX = 'FLUX_BEST'
FWHM = 'FWHM_IMAGE'
FLAGS = 'FLAGS'

COLUMNS = [X, Y, FLUX, FWHM, FLAGS]


class Spalipy:
    """
    Detection-based astronomical image registration.

    Parameters
    ----------
    source_cat, template_cat : str or :class:`astropy.table.Table`
        The detection catalogue for the images. If `str` they should
        be the filenames of the SExtractor catalogues.
    source_fits : :class:`astropy.io.fits.hdu.hdulist.HDUList` or str
        The source image to be transformed.
    shape : None, str or :class:`astropy.io.fits.hdu.hdulist.HDUList`,
        optional
        The shape of the output image. If None, output shape is the
        same as `source_fits`. Otherwise pass a fits filename or
        class:`astropy.io.fits` instance and will take the shape from
        data in `hdu` in that fits file.
    hdu : int or str, optional
        The data in extension `hdu` of `source_fits` will be transformed.
        Also the hdu from which the shape is determined if `shape`
        specifies a fits file.
    ndets : int or float, optional
        The number of detections to use in the initial quad
        determination and detection matching. If 0 < `ndets` < 1 then
        will use this fraction of the shortest of `source_cat` and
        `template_cat` as `ndets`.
    nquaddets : int, optional
        The number of detections to make quads from.
    minquadsep : float, optional
        Minimum distance in pixels between detections in a quad for it
        to be valid.
    maxmatchdist : float, optional
        Maximum matching distance between coordinates after the
        initial transformation to be considered a match.
    minnmatch : int, optional
        Minimum number of matched dets for the initial transformation
        to be considered sucessful.
    spline_order : int, optional
        The order in `x` and `y` of the final spline surfaces used to
        correct the affine transformation. If `0` then no spline
        correction is performed.
    interp_order : int, optional
        The spline order to use for interpolation - this is passed
        directly to `scipy.ndimage.affine_transform` and
        `scipy.ndimage.interpolation.map_coordinates` as the `order`
        argument. Must be in the range 0-5.
    output_filename : None or str, optional
        The filename to write the transformed source file to. If None
        the file will not be written, the transformed data can be
        still accessed through Spalipy.source_data_transformed.
    overwrite : bool, optional
        Whether to overwrite `output_filename` if it exists.
    quiet : bool, optional
        Do not print extra information about alignment to stdout

    Example
    -------
    s = spalipy.Spalipy("source.cat", "template.cat", "source.fits")
    s.main()
    """

    def __init__(self, source_cat, template_cat, source_fits,
                 shape=None, hdu=0, ndets=0.5, nquaddets=20,
                 minquadsep=50, maxmatchdist=5, minnmatch=200,
                 spline_order=3, interp_order=3, output_filename=None,
                 overwrite=True, quiet=False):

        if isinstance(source_cat, str):
            source_cat = Table.read(source_cat, format='ascii.sextractor')
        self.source_cat_full = source_cat.copy()

        if isinstance(template_cat, str):
            template_cat = Table.read(template_cat, format='ascii.sextractor')
        self.template_cat_full = template_cat.copy()

        if isinstance(source_fits, str):
            source_fits = fits.open(source_fits)
        self.source_fits = source_fits.copy()

        hdr = None
        if isinstance(shape, str):
            hdr = fits.getheader(shape, hdu)
        if isinstance(shape, fits.hdu.hdulist.HDUList):
            hdr = shape[hdu].header
        if shape is None:
            hdr = source_fits[hdu].header
        if hdr is not None:
            shape = (int(hdr['NAXIS1']), int(hdr['NAXIS2']))
        self.shape = shape

        self.hdu = hdu

        if isinstance(ndets, float):
            ntot = min(len(source_cat), len(template_cat))
            ndets = int(ndets * ntot)
        self.ndets = ndets

        if ndets < minnmatch:
            msg = ('ndet ({}) < minnmatch ({}) - will never find a suitable '
                   'transform'.format(ndets, minnmatch))
            raise ValueError(msg)

        self.nquaddets = nquaddets
        self.minquadsep = minquadsep
        self.maxmatchdist = maxmatchdist
        self.minnmatch = minnmatch
        self.spline_order = spline_order
        self.interp_order = interp_order

        self.source_cat = self.trim_cat(source_cat)
        self.source_coo = get_det_coords(self.source_cat)
        self.template_cat = self.trim_cat(template_cat)
        self.template_coo = get_det_coords(self.template_cat)
        self.source_quadlist = []
        self.template_quadlist = []

        self.nmatch = 0
        self.source_matchdets = None
        self.template_matchdets = None
        self.affine_transform = None
        self.spline_transform = None
        self.sbs_x = None
        self.sbs_y = None
        self.source_data_transform = None
        self.final_transform = None

        self.output_filename = output_filename
        self.overwrite = overwrite
        self.quiet = quiet

    def main(self, quiet=None):
        """
        Does everything
        """

        if quiet is None:
            quiet = self.quiet

        self.make_quadlist('source')
        self.make_quadlist('template')

        self.find_affine_transform()
        if self.affine_transform is None:
            print('No initial affine transform found')
            return

        if not quiet:
            print("Matched {} detections within {} pixels with initial affine "
                  "transformation".format(self.nmatch, self.maxmatchdist))
            dx_med, dx_std, dy_med, dy_std = self.get_residuals("affine")
            print("Affine alignment pixel residuals [median (stddev)]: "
                  "x = {:.3f} ({:.3f}), y = {:.3f} ({:.3f})"
                  .format(dx_med, dx_std, dy_med, dy_std))

        if self.spline_order > 0:
            self.find_spline_transform()

        self.align()

        if not quiet and self.final_transform is not None:
            dx_med, dx_std, dy_med, dy_std = self.get_residuals("final")
            print("Final alignment pixel residuals [median (stddev)]: "
                  "x = {:.3f} ({:.3f}), y = {:.3f} ({:.3f})"
                  .format(dx_med, dx_std, dy_med, dy_std))

    def make_quadlist(self, image, nquaddets=None, minquadsep=None):
        """
        Create a list of hashes for "quads" of the brightest sources
        in the detection catalogue.

        Parameters
        ----------
        image : str
            Should be "source" or "template" to indicate for which image to
            determine quadlist for.
        nquaddets : None or int, optional
            The number of detections to make quads from. If `None`, inherits
            from the class instance attribute.
        minquadsep : float, optional
            Minimum distance in pixels between detections in a quad for it
            to be valid. If `None`, inherits from the class instance
            attribute.

        """

        if image == 'source':
            coo = self.source_coo
        elif image == 'template':
            coo = self.template_coo
        else:
            raise ValueError('image must be "source" or "template"')

        if nquaddets is None:
            nquaddets = self.nquaddets
        if nquaddets > len(coo):
            print('restricting nquaddets to {}'.format(len(coo)))
            nquaddets = len(coo)

        if minquadsep is None:
            minquadsep = self.minquadsep

        quadlist = []
        quad_idxs = itertools.combinations(range(nquaddets), 4)
        for quad_idx in quad_idxs:
            combo = coo[quad_idx, :]
            dists = distance.pdist(combo)
            if np.min(dists) > minquadsep:
                quadlist.append(quad(combo, dists))

        if image == 'source':
            self.source_quadlist = quadlist
        elif image == 'template':
            self.template_quadlist = quadlist

    def find_affine_transform(self, maxmatchdist=None, minnmatch=None,
                              maxcands=10, minquaddist=0.005):
        """
        Use the quadlist hashes to determine an initial guess at an affine
        transformation and determine matched detections lists. Then refine
        the transformation using the matched detection lists.

        Parameters
        ----------
        maxmatchdist : None or float, optional
            Maximum matching distance between coordinates after the
            initial transformation to be considered a match. If `None`,
            inherits from the class instance attribute.
        minnmatch : None or int, optional
            Minimum number of matched dets for the initial transformation
            to be considered sucessful. If `None`, inherits from the class
            instance attribute.
        maxcands : int, optional
            Max number of quadlist candidates to loop through to find initial
            transformation.
        minquaddist : float, optional
            Not really sure what this is, just copied from alipy.
        """

        if maxmatchdist is None:
            maxmatchdist = self.maxmatchdist
        if minnmatch is None:
            minnmatch = self.minnmatch

        template_hash = np.array([q[1] for q in self.template_quadlist])
        source_hash = np.array([q[1] for q in self.source_quadlist])

        dists = distance.cdist(template_hash, source_hash)
        minddist_idx = np.argmin(dists, axis=0)
        mindist = np.min(dists, axis=0)
        best = np.argsort(mindist)
        if not np.any(mindist < minquaddist):
            print('No matching quads found below minimum quad distance of {}'
                  .format(minquaddist))
            return

        nmatch = 0
        # Use best initial guess at transformation to get list of matched dets
        for i in range(min(maxcands, len(best))):
            bi = best[i]
            dist = mindist[bi]
            if dist < minquaddist:
                # Get a quick (exact) transformation guess
                # using first two detections
                template_quad = self.template_quadlist[minddist_idx[bi]]
                source_quad = self.source_quadlist[bi]
                transform = calc_affine_transform(source_quad[0][:2],
                                                  template_quad[0][:2])
                nmatch, source_matchdets, template_matchdets = \
                    self.match_dets(transform, maxmatchdist=maxmatchdist)
                if nmatch > minnmatch:
                    # Refine the transformation using the matched detections
                    source_match_coo = get_det_coords(source_matchdets)
                    template_match_coo = get_det_coords(template_matchdets)
                    transform = calc_affine_transform(source_match_coo,
                                                      template_match_coo)
                    # Store the final matched detection tables and transform
                    self.nmatch, self.source_matchdets, self.template_matchdets = \
                        self.match_dets(transform, maxmatchdist=maxmatchdist)
                    self.affine_transform = transform
                    break
        else:
            print('{} matched dets after initial affine transform less than '
                  'minimum required ({})'.format(nmatch, minnmatch))

    def find_spline_transform(self, spline_order=None):
        """
        Determine the residual `x` and `y` offsets between matched coordinates
        after affine transformation and fit 2D spline surfaces to describe the
        spatially-varying correction to be applied.

        spline_order : None or int, optional
            The order in `x` and `y` of the spline surfaces used to correct the
            affine transformation. If `None`, inherits from the class instance
            attribute.
        """

        if spline_order is None:
            spline_order = self.spline_order

        # Get the source, after affine transformation, and template coordinates
        source_coo = self.affine_transform.apply_transform(
            get_det_coords(self.source_matchdets))
        template_coo = get_det_coords(self.template_matchdets)
        # Create splines describing the residual offsets in x and y left over
        # after the affine transformation
        kx = ky = spline_order
        try:
            self.sbs_x = interpolate.SmoothBivariateSpline(
                template_coo[:, 0],
                template_coo[:, 1],
                (template_coo[:, 0] - source_coo[:, 0]),
                kx=kx,
                ky=ky,
            )
            self.sbs_y = interpolate.SmoothBivariateSpline(
                template_coo[:, 0],
                template_coo[:, 1],
                (template_coo[:, 1] - source_coo[:, 1]),
                kx=kx,
                ky=ky,
            )
        except dfitpackError:
            print('scipy.interpolate.SmoothBivariateSpline failed, probably due to no enough sources')
            raise

        # Make a callable to map our coordinates using these splines
        def spline_transform(xy, relative=False):
            # Returns the relative shift of xy coordinates if relative is True,
            # otherwise return the value of the transformed coordinates
            x0 = xy[0]
            y0 = xy[1]
            if relative is True:
                x0 = y0 = 0
            if xy.ndim == 2:
                xy = xy.T
            spline_x_offsets = self.sbs_x.ev(xy[0], xy[1])
            spline_y_offsets = self.sbs_y.ev(xy[0], xy[1])
            new_coo = np.array((x0 - spline_x_offsets,
                                y0 - spline_y_offsets))
            if xy.ndim == 2:
                return new_coo.T
            return new_coo

        self.spline_transform = spline_transform

    def align(self, hdu=None, output_filename=None, overwrite=None):
        """
        Perform the alignment and write the transformed source
        file.
        """

        if hdu is None:
            hdu = self.hdu

        if output_filename is None:
            output_filename = self.output_filename

        if overwrite is None:
            overwrite = self.overwrite

        if self.affine_transform is None:
            print("affine_transform is not defined")
            return

        source_data = self.source_fits[hdu].data.T
        if self.spline_transform is not None:
            def final_transform(xy, inverse=True):
                if inverse:
                    return (self.affine_transform.inverse().apply_transform(xy)
                            + (self.spline_transform(xy, relative=True)))
                else:
                    return (self.affine_transform.apply_transform(xy)
                            - (self.spline_transform(xy, relative=True)))
            self.final_transform = final_transform
            xx, yy = np.meshgrid(np.arange(self.shape[0]),
                                 np.arange(self.shape[1]))
            spline_coords_shift = final_transform(np.array([xx, yy]))
            source_data_transform = map_coordinates(source_data,
                                                    spline_coords_shift,
                                                    order=self.interp_order)
        else:
            matrix, offset = self.affine_transform.inverse().matrix_form()
            source_data_transform = interpolation.affine_transform(
                source_data, matrix, offset=offset, order=self.interp_order,
                output_shape=self.shape).T

        self.source_data_transform = source_data_transform

        if output_filename is not None:
            self.source_fits[hdu].data = source_data_transform
            self.source_fits[hdu].writeto(output_filename,
                                          overwrite=overwrite)

    def get_residuals(self, transform):
        """
        Returns the median and standard deviation of the offsets, after
        transformation, between the source and template coordinates

        transform : str
            Should be "affine" or "final". Determines which transform to use to
            determine residuals for (final = affine + spline correction)
        """

        template_coo = get_det_coords(self.template_matchdets)
        source_coo = get_det_coords(self.source_matchdets)
        if transform == 'affine':
            template_coo_trans = self.affine_transform.inverse().apply_transform(template_coo)
        elif transform == 'final':
            template_coo_trans = self.final_transform(template_coo)
        else:
            print('transform must be one of "affine" or "final"')
            return

        dx = template_coo_trans[:, 0] - source_coo[:, 0]
        dy = template_coo_trans[:, 1] - source_coo[:, 1]

        return np.median(dx), np.std(dx), np.median(dy), np.std(dy)

    def match_dets(self, transform, maxmatchdist=None):
        """
        Match the source and template detections using `transform`

        Parameters
        ----------
        transform : :class:`spalipy.AffineTransform`
            The transformation to use.
        maxmatchdist : None or float, optional
            Maximum matching distance between coordinates after the
            initial transformation to be considered a match. If `None`,
            inherits from the class instance attribute.
        """

        if maxmatchdist is None:
            maxmatchdist = self.maxmatchdist

        source_coo_trans = transform.apply_transform(self.source_coo)

        dists = distance.cdist(source_coo_trans, self.template_coo)
        dists_argsort = np.argsort(dists, axis=1)
        dists_sort = dists[np.arange(np.shape(dists)[0])[:, np.newaxis],
                           dists_argsort]
        # For a match, we require the distance to be within our limit, and
        # that the second nearest object is double that distance. This is a
        # crude method to alleviate double matches, maybe caused by aggressive
        # segmentation in the source catalogues.
        passed = ((dists_sort[:, 0] <= maxmatchdist)
                  & (dists_sort[:, 1] >= 2. * maxmatchdist))

        nmatched = np.sum(passed)
        source_matchdets = self.source_cat[passed]
        template_matchdets = self.template_cat[dists_argsort[passed, 0]]

        return nmatched, source_matchdets, template_matchdets

    def trim_cat(self, cat, minfwhm=2, maxflag=7, minsep=None):
        """
        Trim a detection catalogue based on some SExtractor values.
        Sort this by the brightest objects then cut to the top
        `self.ndets`

        Parameters
        ----------
        cat : :class:`astropy.table.Table`
            The detection catalogue to trim.
        minfwhm : float, optional
            The minimum value of FWHM for a valid source.
        maxflag : int, optional
            The maximum value of FLAGS for a valid source.
        minsep : float, optional
            The minimum separation between coordinates in the catalogue.
            If left as default ``None``, this is set to
            ``2 * self.maxmatchdist``.
        """

        if minsep is None:
            minsep = 2 * self.maxmatchdist

        cat = cat[COLUMNS]
        cat = cat[(cat[FWHM] >= minfwhm)
                  & (cat[FLAGS] <= maxflag)]
        cat.sort(FLUX)
        cat.reverse()
        tree = cKDTree(get_det_coords(cat))
        close_pairs = tree.query_pairs(minsep)
        to_remove = [det for pair in close_pairs for det in pair]
        if to_remove:
            cat.remove_rows(np.unique(to_remove))
        cat = cat[:self.ndets]

        return cat


class AffineTransform:
    """
    Represents an affine transformation consisting of rotation, isotropic
    scaling, and shift. [x', y'] = [[a -b], [b a]] * [x, y] + [c d]

    Parameters
    ----------
    v : tuple, list or array
        The parameters of the matrix describing the affine transformation,
        [a, b, c, d].
    """

    def __init__(self, v):
        self.v = np.asarray(v)

    def inverse(self):
        """Returns the inverse transform"""

        # To represent affine transformations with matrices,
        # we can use homogeneous coordinates.
        homo = np.array([
            [self.v[0], -self.v[1], self.v[2]],
            [self.v[1],  self.v[0], self.v[3]],
            [0.0, 0.0, 1.0]
        ])
        inv = linalg.inv(homo)

        return AffineTransform((inv[0, 0], inv[1, 0], inv[0, 2], inv[1, 2]))

    def matrix_form(self):
        """
        Special output for scipy.ndimage.interpolation.affine_transform
        Returns (matrix, offset)
        """

        return (np.array([[self.v[0], -self.v[1]],
                          [self.v[1], self.v[0]]]), self.v[2:4])

    def apply_transform(self, xy):
        """Applies the transform to an array of x, y points"""

        xy = np.asarray(xy)
        # Can consistently refer to x and y as xy[0] and xy[1] if xy is
        # 2D (1D coords) or 3D (2D coords) if we transpose the 2D case of xy
        if xy.ndim == 2:
            xy = xy.T
        xn = self.v[0]*xy[0] - self.v[1]*xy[1] + self.v[2]
        yn = self.v[1]*xy[0] + self.v[0]*xy[1] + self.v[3]
        if xy.ndim == 2:
            return np.column_stack((xn, yn))
        return np.stack((xn, yn))


def calc_affine_transform(source_coo, template_coo):
    """Calculates the affine transformation"""

    n = len(source_coo)
    template_matrix = template_coo.ravel()
    source_matrix = np.zeros((n*2, 4))
    source_matrix[::2, :2] = np.column_stack((source_coo[:, 0],
                                              - source_coo[:, 1]))
    source_matrix[1::2, :2] = np.column_stack((source_coo[:, 1],
                                               source_coo[:, 0]))
    source_matrix[:, 2] = np.tile([1, 0], n)
    source_matrix[:, 3] = np.tile([0, 1], n)

    if n == 2:
        transform = linalg.solve(source_matrix, template_matrix)
    else:
        transform = linalg.lstsq(source_matrix, template_matrix)[0]

    return AffineTransform(transform)


def get_det_coords(cat):
    cat_arr = cat[X, Y].as_array()
    return cat_arr.view((cat_arr.dtype[0], 2))


def quad(combo, dists):
    """
    Create a hash from a combination of four dets (a "quad").

    References
    ----------
    Based on the algorithm of [L10]_.

    .. [L10] Lang, D. et al. "Astrometry.net: Blind astrometric
    calibration of arbitrary astronomical images", AJ, 2010.
    """

    max_dist_idx = np.argmax(dists)
    orders = [(0, 1, 2, 3),
              (0, 2, 1, 3),
              (0, 3, 1, 2),
              (1, 2, 0, 3),
              (1, 3, 0, 2),
              (2, 3, 0, 1)]
    order = orders[max_dist_idx]
    combo = combo[order, :]
    # Look for matrix transform [[a -b], [b a]] + [c d]
    # that brings A and B to 00 11 :
    x = combo[1, 0] - combo[0, 0]
    y = combo[1, 1] - combo[0, 1]
    b = (x-y) / (x**2 + y**2)
    a = (1/x) * (1 + b*y)
    c = b*combo[0, 1] - a*combo[0, 0]
    d = -(b*combo[0, 0] + a*combo[0, 1])

    t = AffineTransform((a, b, c, d))
    (xC, yC) = t.apply_transform((combo[2, 0], combo[2, 1])).ravel()
    (xD, yD) = t.apply_transform((combo[3, 0], combo[3, 1])).ravel()

    _hash = (xC, yC, xD, yD)
    # Break symmetries if needed
    testa = xC > xD
    testb = xC + xD > 1
    if testa:
        if testb:
            _hash = (1.0-xC, 1.0-yC, 1.0-xD, 1.0-yD)
            order = (1, 0, 2, 3)
        else:
            _hash = (xD, yD, xC, yC)
            order = (0, 1, 3, 2)
    elif testb:
        _hash = (1.0-xD, 1.0-yD, 1.0-xC, 1.0-yC)
        order = (1, 0, 3, 2)
    else:
        order = (0, 1, 2, 3)

    return combo[order, :], _hash


if __name__ == '__main__':
    def shape_type(value):
        if value is None:
            return value
        try:
            open(value)
        except FileNotFoundError:
            try:
                value = tuple(map(int, value.split(',')))
            except ValueError:
                msg = 'shape must be valid filepath or "x_size,y_size"'
                raise argparse.ArgumentTypeError(msg)
            else:
                if len(value) != 2:
                    msg = 'shape must be length-2 comma-separated'
                    raise argparse.ArgumentTypeError(msg)
        return value

    def ndets_type(value):
        try:
            return int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                msg = 'ndets must be int or a float between 0 and 1'
                raise argparse.ArgumentTypeError(msg)
            if (value > 1) or (value <= 0):
                msg = 'ndets as a float must be between 0 and 1'
                raise argparse.ArgumentTypeError(msg)
            return value

    parser = argparse.ArgumentParser(
        description='Detection-based astronomical image registration.',
    )
    parser.add_argument(
        'source_cat',
        type=str,
        help='Filename of the source detection catalogue produced by SExtractor.',
    )
    parser.add_argument(
        'template_cat',
        type=str,
        help='Filename of the template detection catalogue produced by SExtractor',
    )
    parser.add_argument(
        'source_fits',
        type=str,
        help='Filename of the source fits image to transform.',
    )
    parser.add_argument(
        'output_filename',
        type=str, help='Filename to write the transformed source_fits to.',
    )
    parser.add_argument(
        '--shape',
        type=shape_type,
        default=None,
        help='Shape of the output transformed image - either filename of '
             'fits file to determine shape from or a "x,y" string.',
    )
    parser.add_argument(
        '--hdu',
        type=int,
        default=0,
        help='the hdu in source_fits to transform the data of. Also the hdu used to derive shape if shape '
             'is a fits filename.',
    )
    parser.add_argument(
        '--ndets',
        type=ndets_type,
        default=0.5,
        help='Number of detections to use when creating quads and detection matching. If  0 < ndets < 1 then will use '
             'this fraction of the shortest of source_cat and template_cat as ndets.',
    )
    parser.add_argument(
        '--nquaddets',
        type=int,
        default=15,
        help='Number of detections to make quads from.',
    )
    parser.add_argument(
        '--minquadsep',
        type=float,
        default=50,
        help='Minimum disance in pixels between detections in a quad for it to be valid.',
    )
    parser.add_argument(
        '--maxmatchdist',
        type=float,
        default=5,
        help='Minimum matching distance between coordinates after the initial transformation to be considered a match.',
    )
    parser.add_argument(
        '--minnmatch',
        type=int,
        default=200,
        help='Minimum number of matched dets for the initial transformation to be considered sucessful.',
    )
    parser.add_argument(
        '--spline-order',
        type=int,
        default=3,
        dest='spline_order',
        help='The order in `x` and `y` of the spline surfaces used ' 'to correct the affine transformation.',
    )
    parser.add_argument(
        '--overwrite',
        action='store_true',
        help='Whether to overwrite output_filename if it exists',
    )

    args_dict = vars(parser.parse_args())

    print('Calling spalipy with:')
    print(args_dict)
    s = Spalipy(**args_dict)
    s.main()
