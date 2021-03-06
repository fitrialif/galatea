"""
Notes:
    1) Code depends on the latest version of Theano
        (you need my pull request to fix Rop and the new interface of scan
    2) Dependency on pylearn2 was dropped, though the code was meant to be
        part of pylearn2

This code implements nonlinear conjugate gradient on the natural gradient

Razvan Pascanu
"""

import numpy
import theano.tensor as TT
import theano
from theano.sandbox.scan import scan
#from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
from theano.tensor.shared_randomstreams import RandomStreams
#from pylearn2.optimization import minres
#from pylearn2.optimization import scalar_armijo_search, scalar_search_wolfe2
from linesearch import scalar_armijo_search, scalar_search_wolfe2
#from pylearn2.utils import constant as const
import minres
import time
from theano.ifelse import ifelse
from theano.sandbox.cuda.basic_ops import gpu_alloc, gpu_from_host
from optimize import linesearch
#from scipy.optimize.linesearch import scalar_search_wolfe1, \
#                                      scalar_search_wolfe2, \
#                                      scalar_search_armijo

gpu_mode = theano.Mode(linker='vm')
cpu_mode = theano.Mode(linker='cvm').excluding('gpu')

def const(value):
    return TT.constant(numpy.asarray(value, dtype=theano.config.floatX))



class natNCG(object):
    def __init__(self, options, channel, data, model):
        """
        Parameters:
            options: Dictionary
            `options` is expected to contain the following keys:
                `cbs` -> int
                    Number of samples to consider at a time when computing
                    some property of the model
                `gbs` -> int
                    Number of samples over which to compute the gradients
                `mbs` -> int
                    Number of samples over which to compute the metric
                `ebs` -> int
                    Number of samples over which to evaluate the training
                    error
                `mreg` -> float
                    Regularization added to the metric
                `mrtol` -> float
                    Relative tolerance for inverting the metric
                `miters` -> int
                    Number of iterations
                `seed` -> int
                    Random number generator seed
                `profile` -> bool
                    Flag, if profiling should be on or not
                `verbose` -> int
                    Verbosity level
                `lr` -> float
                    Learning rate
            channel: jobman channel or None
            data: dictionary-like object return by numpy.load containing the
                data
            model : model
        """
        n_params = len(model.params)
        self.data = data
        self.model = model
        eps = numpy.float32(1e-24)
        xdata = theano.shared(data['train_x'], name='xdata')
        print_mem('xdata')
        self.ydata = theano.shared(data['train_y'], name='ydata')
        self.xdata = xdata
        self.shared_data = [xdata, self.ydata]

        self.options = options

        self.rng = numpy.random.RandomState(options['seed'])
        n_samples = data['train_x'].shape[0]
        self.n_samples = n_samples
        self.grad_batches = n_samples // options['gbs']
        self.metric_batches = n_samples // options['mbs']
        self.eval_batches = n_samples // options['ebs']

        self.verbose = options['verbose']
        # Store eucledian gradients
        cst = time.time()
        self.gs = [theano.shared(numpy.zeros(shp, dtype=theano.config.floatX),
                             name ='g%d'%idx)
                   for idx, shp in enumerate(model.params_shape)]
        # Store riemannian gradients
        self.rs = [theano.shared(numpy.zeros(shp, dtype=theano.config.floatX),
                             name='r%d'%idx)
                   for idx, shp in enumerate(model.params_shape)]
        # Store jacobi diagonal
        self.js = [theano.shared(numpy.zeros(shp, dtype=theano.config.floatX),
                             name='j%d'%idx)
                   for idx, shp in enumerate(model.params_shape)]
        # Store current direction
        self.ds = [theano.shared(numpy.zeros(shp, dtype=theano.config.floatX),
                             name='d%d'%idx)
                   for idx,shp in enumerate(model.params_shape)]


        self.norm_km1km1 = TT.sharedvar.scalar_constructor(numpy.float32(0.),
                                name='nkm1km1')
        self.norm_dkm1 = TT.sharedvar.scalar_constructor(numpy.float32(0.),
                              name = 'dkm1')
        norm_d = TT.sharedvar.scalar_constructor(numpy.float32(1.),
                                                 name='normd')
        self.norm_d = norm_d

        self.permg = self.rng.permutation(self.grad_batches)
        self.permr = self.rng.permutation(self.metric_batches)
        self.perme = self.rng.permutation(self.eval_batches)
        self.k = 0
        self.posg = 0
        self.posr = 0
        self.pose = 0
        self.device = options['device']
        if self.device == 'gpu':
            self.init_gpu(options, channel, data, model)
        else:
            raise Exception('%s mode is not supported anymore'%self.device)


    def init_gpu(self, options, channel, data, model):
        # Step 1. Compile function for computing eucledian gradients
        eps = numpy.float32(1e-24)
        gbdx = TT.iscalar('grad_batch_idx')
        n_params = len(self.model.params)
        print 'Constructing grad function'
        loc_inputs = [x.type() for x in model.inputs]
        srng = RandomStreams(numpy.random.randint(1e5))

        loc_inputs = [x.type() for x in model.inputs]
        def grad_step(*args):
            idx = TT.cast(args[0], 'int32')
            nw_inps = [x[idx * options['cbs']: \
                         (idx + 1) * options['cbs']]
                       for x in loc_inputs]
            replace = dict(zip(model.inputs, nw_inps))
            nw_cost = safe_clone(model.train_cost, replace=replace)
            gs = TT.grad(nw_cost, model.params)
            nw_gs = [op + np for op, np in zip(args[1: 1 + n_params], gs)]
            # Compute jacobi
            nw_outs = safe_clone(model.outs, replace=replace)
            final_results = dict(zip(model.params, [None]*n_params))
            for nw_out, out_operator in zip(nw_outs, model.outs_operator):
                if out_operator == 'sigmoid':
                    denom = numpy.float32(options['cbs'])
                    #denom *= nw_out
                    #denom *= (numpy.float32(1) - nw_out)
                elif out_operator == 'softmax':
                    denom = numpy.float32(options['cbs'])
                    denom *= (nw_out+eps)
                else:
                    denom = numpy.float32(options['cbs'])
                factor = TT.sqrt(numpy.float32(1) / denom)
                if out_operator == 'sigmoid':
                    tnwout = TT.nnet.sigmoid(nw_out)
                    factor = TT.sqrt(tnwout * (numpy.float32(1) -
                                               tnwout))*factor
                r = TT.sgn(srng.normal(nw_out.shape))
                r = r * factor
                loc_params = [x for x in model.params if
                              x in theano.gof.graph.inputs([nw_out])]
                jvs = TT.Lop(nw_out, loc_params, r)
                for lp, lj in zip(loc_params, jvs):
                    if final_results[lp] is None:
                        final_results[lp] = TT.sqr(lj)
                    else:
                        final_results[lp] = final_results[lp] + TT.sqr(lj)
            nw_js = [oj + final_results[p] for oj, p in
                     zip(args[1+n_params:1+2*n_params], model.params)]
            return [args[0] + const(1)] + nw_gs + nw_js

        ig = [TT.unbroadcast(TT.alloc(const(0), 1, *shp),0)
              for shp in model.params_shape]
        ij = [TT.unbroadcast(TT.alloc(const(options['jreg']), 1, *shp),0)
              for shp in model.params_shape]
        idx0 = TT.unbroadcast(const([0]),0)
        n_steps = options['gbs'] // options['cbs']
        rvals, updates = scan(grad_step,
                              states=[idx0] + ig + ij,
                              n_steps=n_steps,
                              name='grad_loop',
                              mode=gpu_mode,
                              profile=options['profile'])

        nw_gs = [x[0] / const(n_steps) for x in rvals[1: 1 + n_params]]
        nw_js = [x[0] for x in rvals[1+n_params:1+2*n_params]]
        updates.update(dict(zip(self.gs + self.js, nw_gs + nw_js)))
        grad_inps = [(x, y[gbdx*options['gbs']:(gbdx+1)*options['gbs']])
                     for x,y in zip(loc_inputs, self.shared_data)]
        print 'Compiling grad function'
        self.compute_eucledian_gradients = theano.function(
            [gbdx],
            [],
            updates=updates,
            givens=dict(grad_inps),
            allow_input_downcast=True,
            name='compute_eucledian_gradients',
            mode=gpu_mode,
            profile=options['profile'])

        # Step 2. Compile function for Computing Riemannian gradients
        rbdx = TT.iscalar('riemmanian_batch_idx')
        def compute_Gv(*args):
            idx0 = const([0])
            ep = [TT.alloc(const(0), 1, *shp)
                  for shp in model.params_shape]

            def Gv_step(*gv_args):
                idx = TT.cast(gv_args[0], 'int32')
                nw_inps = [x[idx * options['cbs']: \
                             (idx + 1) * options['cbs']] for x in
                           loc_inputs]
                replace = dict(zip(model.inputs, nw_inps))
                nw_outs = safe_clone(model.outs, replace)
                final_results = dict(zip(model.params, [None] * len(model.params)))
                for nw_out, out_operator in zip(nw_outs, model.outs_operator):
                    loc_params = [x for x in model.params
                                  if x in theano.gof.graph.inputs([nw_out])]
                    loc_args = [x for x, y in zip(args, model.params)
                                if y in theano.gof.graph.inputs([nw_out])]
                    if out_operator == 'softmax':
                        factor = const(options['cbs']) * (nw_out + eps)
                    elif out_operator == 'sigmoid':
                        factor = const(options['cbs'])# * nw_out * (1 - nw_out)
                    else:
                        factor = const(options['cbs'])

                    if out_operator != 'sigmoid':
                        loc_Gvs = TT.Lop(nw_out, loc_params,
                                     TT.Rop(nw_out, loc_params, loc_args) /\
                                     factor)
                    else:
                        tnwout = TT.nnet.sigmoid(nw_out)
                        loc_Gvs = TT.Lop(nw_out, loc_params,
                                         TT.Rop(nw_out, loc_params,
                                                loc_args) *\
                                         tnwout * (1 - tnwout)/ factor)

                    for lp, lgv in zip(loc_params, loc_Gvs):
                        if final_results[lp] is None:
                            final_results[lp] = lgv
                        else:
                            final_results[lp] += lgv

                Gvs = [ogv + final_results[param]
                       for (ogv, param) in zip(gv_args[1:], model.params)]
                return [gv_args[0] + const(1)] + Gvs
            states = [idx0] + ep
            n_steps = options['mbs'] // options['cbs']
            rvals, updates = scan(Gv_step,
                                  states=states,
                                  n_steps=n_steps,
                                  mode=theano.Mode(linker='cvm'),
                                  name='Gv_step',
                                  profile=options['profile'])

            final_Gvs = [x[0] / const(n_steps) for x in rvals[1:]]
            return final_Gvs, updates
        print 'Constructing riemannian gradient function'
        norm_grads = TT.sqrt(sum(TT.sum(x ** 2) for x in self.gs))
        self.damping = theano.shared(numpy.float32(options['mreg']))
        rvals = minres.minres(
            compute_Gv,
            [x / norm_grads for x in self.gs],
            Ms = self.js,
            rtol=options['mrtol'],
            shift=self.damping,
            maxit=options['miters'],
            profile=options['profile'])
        nw_rs = [x * norm_grads for x in rvals[0]]
        flag = rvals[1]
        niters = rvals[2]
        rel_residual = rvals[3]
        rel_Aresidual = rvals[4]
        Anorm = rvals[5]
        Acond = rvals[6]
        xnorm = rvals[7]
        Axnorm = rvals[8]
        updates = rvals[9]

        norm_ord0 = TT.max(abs(nw_rs[0]))
        for r in nw_rs[1:]:
            norm_ord0 = TT.maximum(norm_ord0,
                                   TT.max(abs(r)))

        reset = TT.scalar(dtype='int8', name='reset')

        norm_kkm1 = sum([(r*g).sum() for r,g in zip(self.rs, self.gs)])
        norm_kk = sum([(r*g).sum() for r,g in zip(nw_rs, self.gs)])
        norm_dk = sum([(d*g).sum() for d,g in zip(self.ds, self.gs)])

        norm_y = norm_kk - 2*norm_kkm1 + self.norm_km1km1
        beta_k = (norm_kk - norm_kkm1)/(norm_dk - self.norm_dkm1) - \
                2 * norm_y * (norm_dk/((norm_dk - self.norm_dkm1) **2))
        beta_k = TT.switch(reset, TT.constant(numpy.float32(0.)),
                           beta_k)
        beta_k = TT.switch(TT.bitwise_or(TT.isnan(beta_k),
                                         TT.isinf(beta_k)),
                           TT.constant(numpy.float32(0.)),
                           beta_k)

        nwds = [-r + beta_k*d for r,d in zip(nw_rs, self.ds)]
        self.nwds = nwds
        nw_normd = TT.sqrt(sum([(d*d).sum() for d in nwds])) + \
                numpy.float32(1e-25)

        updates.update(dict(zip(self.rs, nw_rs)))
        updates.update(dict(zip(self.ds, nwds)))
        updates[self.norm_km1km1] = norm_kk
        updates[self.norm_dkm1] = norm_dk
        updates[self.norm_d] = nw_normd
        print 'Compiling riemannian gradient function'
        cst = time.time()
        grad_inps = [(x, y[rbdx*options['mbs']:(rbdx+1)*options['mbs']])
                     for x,y in zip(loc_inputs, self.shared_data)]
        self.compute_riemannian_gradients = theano.function(
            [reset, rbdx],
            [flag,
             niters,
             rel_residual,
             rel_Aresidual,
             Anorm,
             Acond,
             xnorm,
             Axnorm,
             norm_grads,
             norm_ord0,
             beta_k],
            updates=updates,
            allow_input_downcast = True,
            givens=dict(grad_inps),
            name='compute_riemannian_gradients',
            mode=cpu_mode,
            on_unused_input='warn',
            profile=options['profile'])

        print 'Time to compile Riemannian', print_time(time.time() - cst)
        cst = time.time()
        # Step 3. Compile function for evaluating cost and updating
        # parameters
        print 'constructing evaluation function'
        lr = TT.scalar('lr')
        newparams = [p + lr * d for p, d in zip(model.params, self.ds)]
        nw_ds = [ -r for r in self.rs]
        nw_normd = TT.sqrt(sum([(r*r).sum() for r in self.rs]))
        self.update_params = theano.function([lr],
                                             updates = dict(zip(model.params,
                                                                newparams)),
                                             name='update_params',
                                             on_unused_input='warn',
                                             allow_input_downcast=True,
                                             mode=gpu_mode,
                                             profile=options['profile'])
        self.reset_directions = theano.function([],
                                                updates=dict(zip(self.ds +
                                                                 [self.norm_d],
                                                                 nw_ds +
                                                                 [nw_normd])),
                                                name='reset_dirs',
                                                on_unused_input='warn',
                                                mode=cpu_mode,
                                                allow_input_downcast=True,
                                                profile=options['profile'])

        n_steps = options['ebs'] // options['cbs']
        def ls_cost_step(_idx, acc):
            idx = TT.cast(_idx, 'int32')
            nw_inps = [x[idx * options['cbs']: \
                         (idx + 1) * options['cbs']] for x in loc_inputs]
            replace = dict(zip(model.inputs + model.params,
                               nw_inps + newparams))
            nw_cost = safe_clone(model.train_cost, replace=replace)
            return [_idx + const(1),
                    acc + nw_cost]

        states = [TT.constant(numpy.float32([0])),
                  TT.constant(numpy.float32([0]))]
        rvals, _ = scan(ls_cost_step,
                        states = states,
                        n_steps = n_steps,
                        name='ls_cost_step',
                        profile = options['profile'])
        fcost = rvals[1][0] / const(n_steps)

        def ls_grad_step(_idx, gws):
            idx = TT.cast(_idx, 'int32')
            nw_inps = [x[idx * options['cbs']: (idx + 1) * options['cbs']]
                       for x in loc_inputs]
            replace = dict(zip(model.inputs + model.params,
                               nw_inps + newparams))
            nw_cost = safe_clone(model.train_cost, replace=replace)
            nw_gs = TT.grad(nw_cost, lr)
            return _idx + numpy.float32(1), gws + nw_gs

        states = [TT.constant(numpy.float32([0])),
                  TT.constant(numpy.float32([0]))]
        rvals, _ = scan(ls_grad_step,
                        states = states,
                        n_steps = n_steps,
                        name = 'ls_grad_step',
                        profile=options['profile'])

        fgrad = rvals[1][0] / const(n_steps)
        ebdx = TT.iscalar('ebdx')
        grad_inps = [(x, y[ebdx * options['ebs']:
                           (ebdx + 1) * options['ebs']])
                     for x,y in zip(loc_inputs, self.shared_data)]
        self.ls_cost_fn = theano.function(
            [lr, ebdx],
            fcost,
            givens = grad_inps,
            allow_input_downcast=True,
            name='ls_cost_fn',
            mode=gpu_mode,
            profile=options['profile'])

        self.approx_change = theano.function(
                [lr],
                -lr*sum([TT.sum(g*r) for g,r in zip(self.gs, self.ds)]),
                allow_input_downcast=True,
                name='approx_change',
                mode=gpu_mode,
                profile=options['profile'])


        self.ls_grad_fn = theano.function(
            [lr, ebdx],
            fgrad,
            allow_input_downcast=True,
            givens = grad_inps,
            name='ls_grad_fn',
            mode=gpu_mode,
            profile=options['profile'])

        self.old_score = 50000
        n_steps = options['ebs']// options['cbs']

        def ls_error(_idx, acc):
            idx = TT.cast(_idx, 'int32')
            nw_inps = [x[idx * options['cbs']: \
                         (idx + 1) * options['cbs']] for x in loc_inputs]
            replace = dict(zip(model.inputs, nw_inps))
            nw_cost = TT.cast(safe_clone(
                model.err, replace=replace), 'float32')
            return [_idx + const(1),
                    acc + nw_cost]

        states = [TT.constant(numpy.float32([0])),
                  TT.constant(numpy.float32([0]))]
        rvals, _ = scan(ls_error,
                        states = states,
                        n_steps = n_steps,
                        name='ls_err_step',
                        mode=gpu_mode,
                        profile = options['profile'])
        ferr = rvals[1][0] / const(n_steps)
        self.compute_error = theano.function([ebdx],
                           ferr,
                           givens=dict(grad_inps),
                           name='compute_err',
                           mode=cpu_mode,
                           allow_input_downcast=True,
                           on_unused_input='warn',
                           profile=options['profile'])
        print 'Compile eval time', print_time(time.time() - cst)
        self.old_cost = 1e6
        self.options = options
        self.perm = self.rng.permutation(4)
        self.pos = 0

    def find_optimum(self, pos):
        # line search routine
        ls_cost = lambda x: self.ls_cost_fn(x, pos)
        ls_grad = lambda x: self.ls_grad_fn(x, pos)

        derphi0 = ls_grad(numpy.float32(0))
        phi0 = ls_cost(numpy.float32(0))
        aopt, score, _ = linesearch.scalar_search_wolfe1(
                                ls_cost,
                                ls_grad,
                                phi0 = phi0,
                                derphi0 = derphi0)

        if aopt is None:
            print 'Switching to python wolfe2'
            aopt, score, _, _ = linesearch.scalar_search_wolfe2(
                                ls_cost,
                                ls_grad,
                                phi0 = phi0,
                                derphi0=derphi0)

        use_armijo = False
        try:
            use_armijo = (score > self.old_score*2. or
                          aopt is None or
                          score is None or
                          numpy.isnan(score) or
                          numpy.isinf(score) or
                          numpy.isinf(aopt) or
                          numpy.isinf(aopt))
        except:
            use_armijo = True

        if use_armijo:
            print 'Trying armijo linesearch'
            aopt, score = linesearch.scalar_search_armijo(
                        ls_cost, phi0=phi0, derphi0=derphi0)
        if aopt is None or score is None:
            score = numpy.nan
        else:
            self.old_score = score

        tmp = self.approx_change(numpy.float32(aopt*.5))
        rho = (phi0 - ls_cost(numpy.float32(aopt*.5)))/ tmp
        return score, aopt, rho

    def call_gpu(self, posg, posr, pose):
        n_samples = self.n_samples
        self.move_time = 0
        self.g_time = 0
        mbs = self.options['mbs']
        st = time.time()
        gbs = self.options['gbs']
        ebs = self.options['ebs']
        self.g_st = time.time()
        reset_flag = 0
        self.compute_eucledian_gradients(self.permg[self.posg])
        self.g_time += time.time() - self.g_st
        st = time.time()
        self.r_st = time.time()
        if self.k == 0 or self.k % self.options['resetFreq'] == 0 or \
                reset_flag:
            print 'Reseting direction'
            #self.damping.set_value(numpy.float32(64.))
            self.rvals = self.compute_riemannian_gradients(
                numpy.int8(1),
                self.permr[self.posr])
        else:
            #self.damping.set_value(numpy.float32(self.options['mreg']*self.old_norm))
            self.rvals = self.compute_riemannian_gradients(
                numpy.int8(0),
                self.permr[self.posr])

        self.old_norm = self.rvals[4] + 1.
        self.r_ed = time.time()
        st = time.time()
        self.e_st = time.time()
        self.cost, self.step, self.rho = self.find_optimum(self.perme[self.pose])
        if self.options['adaptivedamp'] == 1:
            if self.rho < .25 or not numpy.isfinite(self.rho):
                reset_flag=1
            else:
                reset_flag=0
            if self.rho < .25 or not numpy.isfinite(self.rho):

                self.damping.set_value(numpy.float32(
                    self.damping.get_value()*3./2.))
            elif self.rho > .75:
                self.damping.set_value(numpy.float32(
                    self.damping.get_value()*2./3.))

        if self.cost > self.old_cost * 3. or numpy.isnan(self.cost) or \
           numpy.isinf(self.cost) or numpy.isnan(self.step) or \
           numpy.isinf(self.step):
            self.reset_directions()
            if self.verbose > 2:
                print 'Reseting direction', self.cost, self.old_cost
            self.cost, self.step, self.rho = self.find_optimum(self.perme[self.pose])

            if self.options['adaptivedamp'] == 1:
                if self.rho < .25 or not numpy.isfinite(self.rho):
                    reset_flag=1
                else:
                    reset_flag=0
                if self.rho < .25 or not numpy.isfinite(self.rho):

                    self.damping.set_value(numpy.float32(
                        self.damping.get_value()*3./2.))
                elif self.rho > .75:
                    self.damping.set_value(numpy.float32(
                        self.damping.get_value()*2./3.))

        self.update_params(self.step)
        self.old_cost = self.cost
        self.e_ed = time.time()
        st = time.time()
        self.error = self.compute_error(self.perme[self.pose])
        self.comp_error_time = time.time() - st


    def __call__(self):
        """
        returns: dictionary
            the dictionary contains the following entries:
                'cost': float - the cost evaluted after current step
                'time_grads': time wasted to compute gradients
                'time_metric': time wasted to compute the riemannian
                     gradients
                'time_eval': time wasted to evaluate function
                'minres_flag': flag indicating the output of minres
                'minres_iters': number of iteration done by minres
                'minres_relres': relative error of minres
                'minres_Anorm': norm of the metric
                'minres_Acond': condition number of the metric
                'grad_norm': gradient norm
                'beta': beta factor for directions in conjugate gradient
                'lambda: lambda factor
        """
        if self.posg == self.grad_batches:
            self.permg = self.rng.permutation(self.grad_batches)
            self.posg = 0
        if self.posr == self.metric_batches:
            self.permr = self.rng.permutation(self.metric_batches)
            self.posr = 0
        if self.pose == self.eval_batches:
            self.perme = self.rng.permutation(self.eval_batches)
            self.pose = 0
        if self.device == 'gpu':
            self.call_gpu(self.permg[self.posg],
                          self.permr[self.posr],
                          self.perme[self.pose])


        if self.verbose > 1:
            print 'Minres: %s' % minres.msgs[self.rvals[0]], \
                        '# iters %04d' % self.rvals[1], \
                        'relative error residuals %10.8f' % self.rvals[2], \
                        'Anorm', self.rvals[4], 'Acond', self.rvals[5], 'beta_k',\
                        self.rvals[-1]
        if self.verbose > 0:
            msg = ('.. iter %4d score %8.5f, error %8.5f, '
                   'step %12.9f '
                   'rho %12.9f damping %12.9f '
                   'ord0_norm %6.3f '
                   'time [host_gpu] %s'
                   '[grad] %s,'
                   '[riemann grad] %s,'
                   '[updates param] %s,'
                   '[error] %s,'
                   '%2d(%2d/%2d) [bsr] %2d(%2d/%2d)[bse]')
            print msg % (
                self.k,
                self.cost,
                self.error,
                self.step,
                self.rho,
                self.damping.get_value(),
                self.rvals[-2],
                print_time(self.move_time),
                print_time(self.g_time),
                print_time(self.r_ed - self.r_st),
                print_time(self.e_ed - self.e_st),
                print_time(self.comp_error_time),
                self.permr[self.posr],
                self.posr + 1,
                self.metric_batches,
                self.perme[self.pose],
                self.pose + 1,
                self.eval_batches)

        self.k += 1
        self.pose += 1
        self.posg += 1
        self.posr += 1
        self.pos += 1
        ret = {
            'score': self.cost,
            'error': self.error,
            'time_err' : self.comp_error_time,
            'time_grads': self.g_time,
            'time_metric': self.r_ed - self.r_st,
            'time_eval': self.e_ed - self.e_st,
            'minres_flag': self.rvals[0],
            'minres_iters': self.rvals[1],
            'minres_relres': self.rvals[2],
            'minres_Anorm': self.rvals[4],
            'minres_Acond': self.rvals[5],
            'grad_norm': self.rvals[-2],
            'beta': self.rvals[-1],
            'rho':self.rho,
            'damping' : self.damping.get_value(),
            'lambda': numpy.float32(0)}
        return ret


def safe_clone(cost, replace):
    params = replace.keys()
    nw_vals = replace.values()
    dummy_params = [x.type() for x in params]
    dummy_cost = theano.clone(cost,
                              replace=dict(zip(params, dummy_params)))
    return theano.clone(dummy_cost,
                        replace=dict(zip(dummy_params, nw_vals)))


def print_time(secs):
    if secs < 120.:
        return '%6.3f sec' % secs
    elif secs <= 60*60:
        return '%6.3f min' % (secs / 60.)
    else:
        return '%6.3f h  ' % (secs / 3600.)

def print_mem(context=None):
    if theano.sandbox.cuda.cuda_enabled:
        rvals = theano.sandbox.cuda.cuda_ndarray.cuda_ndarray.mem_info()
        # Avaliable memory in Mb
        available = float(rvals[0]) / 1024. / 1024.
        # Total memory in Mb
        total = float(rvals[1]) / 1024. / 1024.
        if context == None:
            print ('Used %.3f Mb Free  %.3f Mb, total %.3f Mb' %
                   (total - available, available, total))
        else:
            info = str(context)
            print (('GPU status : Used %.3f Mb Free %.3f Mb,'
                    'total %.3f Mb [context %s]') %
                    (total - available, available, total, info))

class FakeGPUShell(theano.gof.Op):
    def __init__(self, args, fn, n_params):
        self.args = args
        self.fn = fn
        self.n_params = n_params

    def __hash__(self):
        # Diff ?
        return hash(type(self))

    def __eq__(self, other):
        # Diff ?
        return type(self) == type(other)

    def __str__(self):
        return self.__class__.__name__

    def make_node(self, *args):
        return theano.gof.Apply(self, args, [x.type() for x in args[:self.n_params]])

    def perform(self, node, inputs, outputs):
        for vb, dt in zip(self.args, inputs[:self.n_params]):
            vb.set_value(dt)
        nw_vals =  self.fn(*inputs[self.n_params:])
        for vb, dt in zip(outputs, nw_vals):
            vb[0] = dt

