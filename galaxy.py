

import glob
import argparse
import os
import string
import pdb
import resource
import pyfits as fits
import numpy as np
import matplotlib.pyplot as plt
import scipy.ndimage.interpolation as sp_interp
from math import pi, ceil
from scipy.interpolate import interp1d
from scipy.optimize import fsolve
from astropy.table import Table
from astropy.stats import sigma_clipped_stats
from collections import defaultdict
from random import gauss
from skimage import measure
from photutils import aperture_photometry, EllipticalAnnulus, \
                              EllipticalAperture, CircularAnnulus, \
                              CircularAperture
import utils
import clean
import galaxy_plot

class Galaxy(object):

    def __init__(self, hdulist, filename, flags):
        
        # initialize fits image & catalog data
        image = hdulist['CLN'].data
        segmap = hdulist['FSEG'].data
        cat = hdulist['CAT'].data[hdulist['CLN'].header['SECATIDX']]

        # flags and naming attributes
        self.cat = flags[0]
        self.oflag = flags[1]
        self.uflag = flags[2]
        self.name = os.path.basename(os.path.splitext(filename)[0])
  
        # SExtractor attributes        
        self.e = cat['ELONGATION']
        self.x, self.y = cat['X_IMAGE'], cat['Y_IMAGE']
        self.kron = cat['KRON_RADIUS']
        self.a, self.b = cat['A_IMAGE'], cat['B_IMAGE']
        self.theta = cat['THETA_IMAGE']*pi/180. # in radians?
        self.ra, self.dec = cat['ALPHA_J2000'], cat['DELTA_J2000']

        # background attributes
        self.med, self.rms = self.background(image, segmap)

        # morphological attributes
        self.rp, self.rpflag = self.get_petro(image)
        self.elipt = cat['ELLIPTICITY']            
        if self.rp > 0.:
            self.A, self.Ac = self.get_asymmetry(image)
            self.C = self.get_concentration(image)
            self.G = self.get_gini(image)
            self.M, self.Mc = self.get_m20(image)
        else:
            self.A, self.Ac = np.nan, (self.x, self.y)
            self.C = np.nan
            self.G = np.nan, 
            self.M, self.Mc = np.nan, (self.x, self.y)

        #dir(self) in cmd line to see all the hidden shits

    def __enter__(self):
        return self
      
    def background(self, data, segmap):
        mean, median, std = sigma_clipped_stats(data[segmap==0])
        #median = np.median(data[segmap==0])
        #rms = np.sqrt(np.mean(np.square(data[segmap==0])))
        return median, std
  
    def get_petro(self, image):
        r_flag = 0
       
        # condition of np.log10(imgsize/constant) ensures that the maximum
        # radius will never exceed the size of the image
        a = 10*np.logspace(-1.0, np.log10(image.shape[0]/2./10.), num=20)
        b = a/self.e
        position = [self.x, self.y]

        # determine flux (counts) in annuli at various radii
        annuli_atR = np.hstack([EllipticalAnnulus(position, a[idx-1], a[idx+1], 
                                              b[idx+1], self.theta) \
                                for idx in range(1,len(a[:-1]))])
        counts_atR = np.hstack([aperture_photometry(image, an, method='exact') \
                                for an in annuli_atR])['aperture_sum']

        annuli_inR = np.hstack([EllipticalAnnulus(position, a[idx-1], a[idx], 
                                                  b[idx], self.theta) \
                                for idx in range(1,len(a[:-1]))])
        counts_inR = np.hstack([aperture_photometry(image, an, method='exact') \
                                for an in annuli_inR])['aperture_sum']

        areas_atR = np.array([an.area() for an in annuli_atR])
        areas_inR = np.array([an.area() for an in annuli_inR])

        # surface brightness = total counts / area
        sb = self._sb = counts_atR/areas_atR      

        # average surface brightness is the cumulative sum of the counts within
        # the radius, R, divided by the cumulative area withn in the same R 
        num = len(counts_inR)+1
        csum = np.array([np.sum(counts_inR[0:idx]) for idx in range(1, num)])
        asum = np.array([np.sum(areas_inR[0:idx]) for idx in range(1, num)])
        avgsb = self._avgsb = csum/asum  

        '''
        # estimate error for SB and <SB>
        sberr = self._sberr = np.sqrt(areas1*self.rms**2)/areas1
        avgsberr = self._avgsberr = np.sqrt(asum*self.rms**2)

        # estimate error for the ratio of SB/<SB>
        ratio_err = self._ratio_err = np.sqrt((sberr/sb)**2 + 
                                              (avgsberr/avgsb)**2)
        #'''
        
        # now we need to find the intersection of sb/<sb> with 0.2:
        # define a finer spacing of radii to interpolate onto
        self._rads = a[1:-1]
        radii = self._interprads = np.linspace(np.min(self._rads), 
                                               np.max(self._rads), num=1000)
        f = interp1d(self._rads, sb/avgsb, kind='cubic')
        # values of the ratio SB/<SB> as a function of radius
        ratios = self._interpvals = f(self._interprads)

        if not np.any(np.isnan(ratios)):
            rp = utils.get_intersect(ratios, 0.2, radii, mono='dec')
            if rp == -1:
                r_flag = 1
            return rp, r_flag
        else:
            print "Petrosian interpolation failed!"
            rp, r_flag = -1, 2
            return rp, r_flag
 

    def bkg_asymmetry(self, aperture):

        # create a square background image approx same size 
        # as area of aperture (we need to minimize calculations)
        size = ceil(np.sqrt(ceil(aperture.area()))) 
        bkg_img = np.zeros((size, size))
        mask = np.where(bkg_img == 0)

        #stddev = np.std(image[segmap==0])
        for pixel in zip(mask[0], mask[1]):
            bkg_img[pixel] = gauss(self.med, self.rms)
        
        # save the background image 
        bkg = fits.ImageHDU(data=bkg_img)
        bkg.writeto('output/asymimgs/'+self.name+'.fits', clobber=True)
        #'''

        # minimize the background asymmetry
        ba = []
        for idx1 in range(bkg_img.shape[0]):
            for idx2 in range(bkg_img.shape[1]):
                shifted = bkg_img.take(range(idx1-bkg_img.shape[0], idx1), 
                                       mode='wrap', axis=0) \
                                 .take(range(idx2-bkg_img.shape[1], idx2), 
                                       mode='wrap', axis=1)
                rotated = np.rot90(shifted, 2) 
                ba.append(np.sum(np.abs(shifted-rotated)))

        # find the  minimum of all possible bkg asyms, normalized to the exact
        # area of the original aperture
        bkgasym = np.min(ba)*aperture.area()/(bkg_img.shape[0]*bkg_img.shape[1])
        return bkgasym
        
    def get_asymmetry(self, image):
        '''
        In this one, we're doing it Claudia's way
        1. make a smaller image of the galaxy -> 2*petrosian rad
        2. create a background image
        3. create an aperture 1.5*petrosian radius
        4. minimize asymmetry in the bakground img
        5. minimize asymmetry in the galaxy img

        #'''
        print "calculating Asymmetry..."

        galcenter = np.array([self.x, self.y])
        imgcenter = np.array([image.shape[0]/2., image.shape[1]/2.])
        delta = imgcenter - galcenter

        aper = EllipticalAperture(imgcenter, self.rp, self.rp/self.e,self.theta)
        bkg_asym = self.bkg_asymmetry(aper)

        asyms = defaultdict(list)
        prior_points = []

        #  minimize the galaxy asymmetry
        while True:
            ga = []
            dd = []
            deltas, points = utils.generate_deltas(imgcenter, .3, delta)

            for d, p in zip(deltas, points): 
                # if the point already exists in the dictionary, 
                #don't run asym codes!
                if p not in asyms: 
                    newdata = sp_interp.shift(image, d)
                    rotdata = sp_interp.rotate(newdata, 180.)
                    residual = np.abs(newdata-rotdata)
                    numerator = aperture_photometry(residual, aper)
                    denominator = aperture_photometry(np.abs(newdata), aper)
                    num = float(numerator['aperture_sum']) 
                    den = float(denominator['aperture_sum'])
                    galasym = num/den

                    # create an array of asyms ... 
                    ga.append(galasym)
                    dd.append(den)
                    # ... and a dictionary that maps each asym to a 
                    #point on the image grid
                    asyms[p].append([galasym,den])

                    #galaxy_plot.asym_plot(newdata, residual, aperture, 
                    #                      galcenter, self.rp)

                # just take the value that's already in the dictionary 
                # for that point
                else:
                    ga.append(asyms[p][0][0])
                    dd.append(asyms[p][0][1])

            # if the asymmetry found at the original center 
            # (first delta in deltas) is the minimum, we're done!
            if ga[0] == np.min(ga):
                center = imgcenter - deltas[0]
                # save the residual image 
                res = fits.ImageHDU(data=residual)
                res.writeto('output/asymimgs/'+self.name+'_res.fits', clobber=True)
                #galaxy_plot.asym_plot(newdata, residual, aperture, 
                #                      center, self.rp, self.med, self.rms)
                return ga[0]-bkg_asym/dd[0], center.tolist()
            else:
                minloc = np.where(ga == np.min(ga))[0]
                delta = deltas[minloc[0]]
                prior_points = list(points)


    def get_concentration(self, image):
        '''
        To calculate the conctration we need to find the radius 
            -- which encloses 20% of the total light
            -- which encloses 80% of the total light
        So we need a running sum of the total pixel counts in increasing radii
        We also need to know the total flux -- 
           define as the total pixel counts within 
           an aperture of  one petrosian radius
        Divide the running sum by the fixed total flux value and 
           see where this ratio crosses .2 and .8
        #'''
        print "calculating Concentration..."

        radii = 10*np.logspace(-1.0, np.log10(image.shape[0]/2./10.), num=20)

        # Build circular annuli centered on the ASYMMETRY CENTER of the galaxy
        annuli = [CircularAnnulus((self.Ac[0], self.Ac[1]),radii[i-1],radii[i])\
                  for i in range(1,len(radii))]
        counts = np.hstack([aperture_photometry(image, an, method='exact') \
                            for an in annuli])['aperture_sum']
        cum_sum = np.array([np.sum(counts[0:idx]) \
                            for idx in range(1,len(counts)+1)])

        # Calculate the total flux in a Circular aperture of 1.5*rpet
        tot_aper = CircularAperture((self.Ac[0], self.Ac[1]), self.rp)
        tot_flux = float(aperture_photometry(image, tot_aper, 
                                             method='exact')['aperture_sum'])

        # ratio of the cumulative counts over the total counts in the galaxy
        ratio = cum_sum/tot_flux
        rads = radii[1:]

        # now we need to find the intersection of ratio with 0.2 and 0.8
        interp_radii, interp_ratio = utils.get_interp(rads, ratio)

        if not np.any(np.isnan(interp_ratio)):
            r20 = utils.get_intersect(interp_ratio, 0.2, interp_radii)
            r80 = utils.get_intersect(interp_ratio, 0.8, interp_radii)
        else:
            print "Concentration interpolation failed."
            r20 = r80 = -1
        
        return  5*np.log10(r80/r20)
        
    def get_gini(self, image):
        '''
        Need all pixels associated with a galaxy -- use my aperture thing? 
        1. All pixels within 1 Rp
        2. All pixels above the mean SB at 1 Rp
        Gonna do 1. for  now but want to try 2. as well
        '''
        print "calculating Gini..."

        # Create aperture at center of galaxy 
        # (Using my Aperture, not photutils, because I want access to the 
        # individual pixels/mask -- not just a sum of pixel values
        ap = utils.EllipticalAperture((self.x, self.y), self.rp, self.rp/self.e,
                                      self.theta, image)
        pixels = ap.aper * image
        galpix = pixels[pixels > 0.]

        galpix_sorted = sorted(galpix)
        xbar = np.mean(galpix_sorted)
        n = len(galpix_sorted)

        # calculate G
        gsum = [2*i-n-1 for i, p in enumerate(galpix_sorted)]
        g = 1/(xbar*n*(n-1))*np.dot(gsum, galpix_sorted)

        return g
            
    def get_m20(self, image):

        print "Calculating M20..."
        
        center = [image.shape[0]/2., image.shape[1]/2.]
        galcenter = np.array([self.x, self.y])

        mxrange = [center[0]-round(0.5*self.rp), center[0]+round(0.5*self.rp)]
        myrange = [center[1]-round(0.5*self.rp), center[1]+round(0.5*self.rp)]
        #x = range(mxrange[0], mxrange[1])
        #y = range(myrange[0], myrange[1])
        x, y = np.ogrid[mxrange[0]:mxrange[1], myrange[0]:myrange[1]]

        # Create a "distance grid" - each element's value is it's distance
        # from the center of the image for the entire image
        x2, y2 = np.ogrid[:image.shape[0], :image.shape[1]]      
        dist_grid = (center[0] - x2)**2 + (center[1] - y2)**2

        # create aperture at center of galaxy (mask)
        gal_aper = utils.EllipticalAperture(center, self.rp, self.rp/self.e,
                                            self.theta, image)

        # We want the 0.0 element in dist_grid to correspond to each "test"
        # center that we calculate Mtot on so we have to shift the dist_grid
        # for each calculation of Mtot
        mtots = []
        for i in x:
            for j in y.transpose():
                indices0 = range(center[0]-i, center[0]-i + 
                                     dist_grid.shape[0])
                indices1 = range(center[1]-j, center[1]-j + 
                                     dist_grid.shape[1])
                shift_grid = dist_grid.take(indices0, axis=0, mode='wrap')\
                                      .take(indices1, axis=1, mode='wrap')
                mtots.append(np.sum(gal_aper.aper*image*shift_grid))

        Mtot1 = np.min(mtots)
        mtots = np.array(mtots).reshape([len(x), len(y.transpose())])
        xc = x[np.where(mtots == Mtot1)[0]]
        yc = y.transpose()[np.where(mtots == Mtot1)[1]]

        #grid = dist_grid.take(center[0]-xc, center[0]-xc+dist_grid.shape[0])\
        #                .take(center[1]-yc, center[1]-yc+dist_grid.shape[1])

        print Mtot1, xc, yc
        pdb.set_trace()

        '''
        ##############################################################3
        # Old Method: 
        x = y = np.arange(center[0]-round(0.5*self.rp), 
                          center[0]+round(0.5*self.rp))
        xx, yy = np.meshgrid(x, y)
        # distance grid: 
        x2, y2 = np.ogrid[:image.shape[0], :image.shape[1]]

        dist_grids = [(x2-xi)**2 + (y2-yi)**2 for xi, yi in \
                      zip(xx.flatten(), yy.flatten())]

        # create aperture at center of galaxy (mask)
        gal_aper = utils.EllipticalAperture(center, 
                            self.rp, self.rp/self.e, self.theta, image)

        mtots2 = [np.sum(gal_aper.aper*image*grid) for grid in dist_grids]
        Mtot2 = np.min(mtots2)
        xc2, yc2 = xx.flatten()[mtots2 == Mtot2], yy.flatten()[mtots2 == Mtot2]
        grid2 = dist_grids[np.where(mtots2 == Mtot2)[0]]

        print Mtot2, xc2, yc2
        pdb.set_trace()
        '''       

        # Once we have a minimized mtot and the galcenter that minimizes it
        # we can calculate M20!

        # create aperture/dist map at center that minimizes Mtot
        m20_aper = utils.EllipticalAperture((xc, yc), self.rp, 
                                    self.rp/self.e, self.theta, image)
        galpix = m20_aper.aper*image
        dist_grid_sorted = np.array([x for y, x, in \
                            sorted(zip(galpix.flatten(), dist_grid.flatten()),
                                   reverse=True)])
        galpix_sorted = np.array(sorted(galpix.flatten(), reverse=True))

        ftot20 = 0.2*np.sum(galpix_sorted)
        fcumsum = np.cumsum(galpix_sorted)

        # find where fcumsum is less than ftot20 -- 
        # the elements in this array are those which will be used to compute M20
        m20_pix_idx = np.where(fcumsum < ftot20)[0]
        
        m20_galpix = galpix_sorted[m20_pix_idx]
        m20_distpix = dist_grid_sorted[m20_pix_idx]

        M20 = np.log10(np.sum(m20_galpix*m20_distpix)/Mtot)

        return M20, (xc[0], yc[0])
        
    def table(self): 
        the_dict = self.__dict__
        entries = ['name', '_sb', '_avgsb', '_rads', 'rms', 'med',
                   '_interprads', '_interpvals', '_sberr', '_ratio_err']
        for key in entries:
            if key in the_dict:
                del the_dict[key]

        return the_dict

        
    def __exit__(self, type, value, traceback):
        self.stream.close()



####################### main ############################

def main():
    
    parser = argparse.ArgumentParser(description='Perform LLE/PCA/whatevs')
    parser.add_argument('directory', type=str, 
        help='Directory of fits images on which to run LLE.')
    parser.add_argument('output', type=str,
        help='Specify the desired name for output catalog.')
    args = parser.parse_args()
    
    fitsfiles = sorted(glob.glob(args.directory+'*.fits'))
    #fitsfiles = sorted(glob.glob(args.directory))

    outdir = 'output/datacube/'

    #t = Table(names=('name', 'Fidx', 'Fdist', 'Bdist', 
    #                 'F-B', 'Farea', 'Barea', 'Flag', 'cFlag'), 
    #          dtype=('S70', 'i', 'f4', 'f4', 'f4', 'f4', 'f4', 'i', 'i'))

    t = Table(names=('cat', 'oflag', 'uflag', 'e', 'x', 'y', 
                     'kron', 'a', 'b', 'theta', 'ra', 'dec', 'rp', 'rpflag', 
                     'A', 'Ac', 'C', 'G', 'Gflag', 'M', 'Mc', 'elipt'), 
              dtype=('i', 'i', 'i', 'f4', 'f4', 'f4', 'f4', 'f4', 'f4', 'f4', 
                     'f4', 'f4', 'f4', 'i', 'f4', ('f4', (2,)), 'f4', 'f4', 
                     'i', 'f4', ('f4', (2,)), 'f4'))

    for f in fitsfiles: 
        basename = os.path.basename(f)
        filename = outdir+'f_'+basename
        if not os.path.isfile(filename):
            print "File not found! Running SExtractor before proceeding."
            print "Cleaning ", os.path.basename(f)
        flags = clean.clean_frame(f, outdir)

            #t.add_row((basename, fidx, fdist, bdist, fbdist, 
            #           farea, barea, flag, cflag))
            #fidx,fdist,bdist,fbdist,farea,barea,
            #pdb.set_trace()
        #else:

        print "Running", os.path.basename(f)

        hdulist = fits.open(filename, memmap=True)
        g = Galaxy(hdulist, filename, flags)
        #utils.resource_getrusage()
        #utils.resource_getrlimits()
        #pdb.set_trace()
        galaxy_plot.plot(g, hdulist)
        t.add_row(g.table())
        hdulist.close()
        del g
        exit()


    t.write('data?.txt', format='ascii.fixed_width', delimiter='')
    #info = Table(rows=[g.__dict__ for g in galaxies])
    #info.write(args.output, overwrite=True)

    exit()  


if __name__ == '__main__':
    main()
    
