#!/usr/bin/env python3
"""imagenette / imagewoofの実験用コード"""
import argparse
import logging
import pathlib

import albumentations as A
import cv2
import numpy as np
import PIL.Image
import PIL.ImageEnhance
import PIL.ImageOps
import sklearn.externals.joblib as joblib
import sklearn.metrics
import tensorflow as tf

# tf.keras or keras。たぶんどっちでも動くつもり。
USE_TF_KERAS = True
if USE_TF_KERAS:
    keras = tf.keras
    import horovod.tensorflow.keras as hvd
else:
    import keras
    import horovod.keras as hvd


def _main():
    try:
        import better_exceptions
        better_exceptions.hook()
    except BaseException:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='imagenette', choices=('imagenette', 'imagewoof'))
    parser.add_argument('--swap-train-val', action='store_true', default=True)
    parser.add_argument('--no-swap-train-val', dest='swap_train_val', action='store_false')
    parser.add_argument('--model', default='resnet', choices=('resnet', 'inception_resnet_v2', 'nasnet'))
    parser.add_argument('--check', action='store_true', help='3epochだけお試し実行(動作確認用)')
    parser.add_argument('--results-dir', default=pathlib.Path('results'), type=pathlib.Path)
    args = parser.parse_args()

    hvd.init()

    handlers = [logging.StreamHandler()]
    if hvd.rank() == 0:
        args.results_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.results_dir / f'{args.data}.{args.model}.log', encoding='utf-8'))
    logging.basicConfig(format='[%(levelname)-5s] %(message)s', level='INFO', handlers=handlers)
    logger = logging.getLogger(__name__)

    config = tf.ConfigProto()
    config.allow_soft_placement = True
    config.gpu_options.allow_growth = True
    config.gpu_options.visible_device_list = str(hvd.local_rank())
    keras.backend.set_session(tf.Session(config=config))

    (X_train, y_train), (X_val, y_val), num_classes = _load_data(args.data, args.swap_train_val)
    input_shape = (331, 331, 3)

    epochs = 5 if args.check else 1800
    batch_size = 8 if args.model == 'nasnet' else 16  # NASNetLargeはメモリがやや厳しい。。
    base_lr = 1e-3 * batch_size * hvd.size()

    model = {
        'resnet': _create_network,
        'inception_resnet_v2': _create_network_inception_resnet_v2,
        'nasnet': _create_network_nasnet,
    }[args.model](input_shape, num_classes)
    optimizer = keras.optimizers.SGD(lr=base_lr, momentum=0.9, nesterov=True)
    optimizer = hvd.DistributedOptimizer(optimizer, compression=hvd.Compression.fp16)
    model.compile(optimizer, 'categorical_crossentropy')
    model.summary(print_fn=logger.info if hvd.rank() == 0 else lambda x: x)
    keras.utils.plot_model(model, args.results_dir / f'{args.data}.{args.model}.svg', show_shapes=True)

    callbacks = [
        _cosine_annealing_callback(base_lr, epochs),
        hvd.callbacks.BroadcastGlobalVariablesCallback(0),
        hvd.callbacks.LearningRateWarmupCallback(warmup_epochs=5, verbose=1),
    ]
    model.fit_generator(_generate(X_train, y_train, batch_size, num_classes, shuffle=True, data_augmentation=True),
                        steps_per_epoch=int(np.ceil(len(X_train) / batch_size / hvd.size())),
                        epochs=epochs,
                        callbacks=callbacks,
                        verbose=1 if hvd.rank() == 0 else 0)

    if hvd.rank() == 0:
        # 検証
        pred_val = model.predict_generator(
            _generate(X_val, np.zeros((len(X_val),), dtype=np.int32), batch_size, num_classes),
            int(np.ceil(len(X_val) / batch_size)),
            verbose=1 if hvd.rank() == 0 else 0)
        logger.info(f'Arguments: --data={args.data} --model={args.model}')
        logger.info(f'Validation Accuracy:      {sklearn.metrics.accuracy_score(y_val, pred_val.argmax(axis=-1)):.3f}')
        logger.info(f'Validation Cross Entropy: {sklearn.metrics.log_loss(y_val, pred_val):.3f}')
        # 後で何かしたくなった時のために一応保存
        model.save(args.results_dir / f'{args.data}.{args.model}.h5', include_optimizer=False)


def _load_data(data, swap_train_val):
    """データの読み込み。"""
    data_dir = pathlib.Path('.') / data
    train_dir = data_dir / 'train'
    val_dir = data_dir / 'val'
    class_names = list(sorted([p.name for p in train_dir.iterdir() if p.is_dir()]))
    class_name_to_id = {cn: i for i, cn in enumerate(class_names)}
    X_train = [x for x in train_dir.glob('**/*') if x.is_file()]
    y_train = [class_name_to_id[x.parent.name] for x in X_train]
    X_val = [x for x in val_dir.glob('**/*') if x.is_file()]
    y_val = [class_name_to_id[x.parent.name] for x in X_val]
    if swap_train_val:
        X_train, X_val = X_val, X_train
        y_train, y_val = y_val, y_train
    return (X_train, y_train), (X_val, y_val), len(class_names)


def _create_network(input_shape, num_classes):
    """ネットワークを作成して返す。"""
    def _create():
        inputs = x = keras.layers.Input(input_shape)
        x = _conv2d(64, 7, strides=2)(x)  # 166
        x = _conv2d(128, strides=2, use_act=False)(x)  # 83
        x = _blocks(128, 2)(x)
        x = _conv2d(256, strides=2, use_act=False)(x)  # 42
        x = _blocks(256, 4)(x)
        x = _conv2d(512, strides=2, use_act=False)(x)  # 21
        x = _blocks(512, 8)(x)
        x = _conv2d(512, strides=2, use_act=False)(x)  # 11
        x = _blocks(512, 4)(x)
        x = keras.layers.GlobalAveragePooling2D()(x)
        x = keras.layers.Dense(num_classes, activation='softmax',
                               kernel_regularizer=keras.regularizers.l2(1e-5),
                               bias_regularizer=keras.regularizers.l2(1e-5))(x)
        model = keras.models.Model(inputs=inputs, outputs=x)
        return model

    def _blocks(filters, count):
        def _layers(x):
            for _ in range(count):
                sc = x
                x = _conv2d(filters, use_act=True)(x)
                x = _conv2d(filters, use_act=False)(x)
                x = keras.layers.add([sc, x])
            x = _bn_act()(x)
            return x
        return _layers

    def _conv2d(filters, kernel_size=3, strides=1, use_act=True):
        def _layers(x):
            x = keras.layers.Conv2D(filters, kernel_size=kernel_size, strides=strides,
                                    padding='same', use_bias=False,
                                    kernel_initializer='he_uniform',
                                    kernel_regularizer=keras.regularizers.l2(1e-5))(x)
            x = _bn_act(use_act=use_act)(x)
            return x
        return _layers

    def _bn_act(use_act=True):
        def _layers(x):
            x = keras.layers.BatchNormalization(gamma_regularizer=keras.regularizers.l2(1e-5))(x)
            # x = MixFeat()(x)
            x = keras.layers.Activation('relu')(x) if use_act else x
            return x
        return _layers

    return _create()


def _create_network_inception_resnet_v2(input_shape, num_classes):
    """ネットワークを作成して返す。InceptionResNetV2。"""
    return keras.applications.InceptionResNetV2(input_shape=input_shape, classes=num_classes, weights=None)


def _create_network_nasnet(input_shape, num_classes):
    """ネットワークを作成して返す。NASNetLarge。"""
    return keras.applications.NASNetLarge(input_shape=input_shape, classes=num_classes, weights=None)


class MixFeat(keras.layers.Layer):
    """MixFeat <https://openreview.net/forum?id=HygT9oRqFX>"""

    def __init__(self, sigma=0.2, **kargs):
        self.sigma = sigma
        super().__init__(**kargs)

    def call(self, inputs, training=None):  # pylint: disable=arguments-differ
        def _passthru():
            return inputs

        def _mixfeat():
            @tf.custom_gradient
            def _forward(x):
                shape = keras.backend.shape(x)
                indices = keras.backend.arange(start=0, stop=shape[0])
                indices = tf.random_shuffle(indices)
                rs = keras.backend.concatenate([keras.backend.constant([1], dtype='int32'), shape[1:]])
                r = keras.backend.random_normal(rs, 0, self.sigma, dtype='float32')
                theta = keras.backend.random_uniform(rs, -np.pi, +np.pi, dtype='float32')
                a = 1 + r * keras.backend.cos(theta)
                b = r * keras.backend.sin(theta)
                y = x * a + keras.backend.gather(x, indices) * b

                def _backword(dx):
                    inv = tf.invert_permutation(indices)
                    return dx * a + keras.backend.gather(dx, inv) * b

                return y, _backword

            return _forward(inputs)

        return keras.backend.in_train_phase(_mixfeat, _passthru, training=training)

    def get_config(self):
        config = {'sigma': self.sigma}
        base_config = super().get_config()
        return dict(list(base_config.items()) + list(config.items()))


def _cosine_annealing_callback(base_lr, epochs):
    """Cosine annealing <https://arxiv.org/abs/1608.03983>"""
    def _cosine_annealing(ep, lr):
        min_lr = base_lr * 0.01
        return min_lr + 0.5 * (base_lr - min_lr) * (1 + np.cos(np.pi * (ep + 1) / epochs))
    return keras.callbacks.LearningRateScheduler(_cosine_annealing)


def _generate(X, y, batch_size, num_classes, shuffle=False, data_augmentation=False):
    """generator。"""
    if data_augmentation:
        aug1 = A.Compose([
            A.Resize(331, 331, p=1),
            A.PadIfNeeded(412, 412),
            _create_autoaugment(),
            A.RandomSizedCrop((265, 412), 331, 331, p=1),
            A.HorizontalFlip(),
        ])
        aug2 = A.Compose([A.Normalize(mean=0.5, std=0.5), A.Cutout(num_holes=1, max_h_size=16, max_w_size=16)])
    else:
        aug1 = A.Compose([A.Resize(331, 331, p=1)])
        aug2 = A.Compose([A.Normalize(mean=0.5, std=0.5)])

    with joblib.Parallel(backend='threading', n_jobs=batch_size) as parallel:
        if shuffle:
            batch_indices = []
            for index in _generate_shuffled_indices(len(X)):
                batch_indices.append(index)
                if len(batch_indices) == batch_size:
                    yield _generate_batch(X, y, aug1, aug2, num_classes, data_augmentation, batch_indices, parallel)
                    batch_indices = []
        else:
            while True:
                for i in range(0, len(X), batch_size):
                    batch_indices = range(i, min(i + batch_size, len(X)))
                    yield _generate_batch(X, y, aug1, aug2, num_classes, data_augmentation, batch_indices, parallel)


def _generate_shuffled_indices(data_count):
    """シャッフルしたindexを無限に返すgenerator。"""
    all_indices = np.arange(data_count)
    while True:
        np.random.shuffle(all_indices)
        yield from all_indices


def _generate_batch(X, y, aug1, aug2, num_classes, data_augmentation, batch_indices, parallel):
    """1バッチずつの処理。"""
    jobs = [_generate_instance(X, y, aug1, aug2, num_classes, data_augmentation, i) for i in batch_indices]
    results = parallel(jobs)
    X_batch, y_batch = zip(*results)
    return np.array(X_batch), np.array(y_batch)


@joblib.delayed
def _generate_instance(X, y, aug1, aug2, num_classes, data_augmentation, index):
    """1サンプルずつの処理。"""
    X_i, y_i = X[index], y[index]
    X_i = aug1(image=_load_image(X_i))['image']
    c_i = _to_categorical(y_i, num_classes)

    if data_augmentation:
        # Between-class Learning
        while True:
            t = np.random.choice(len(y))
            if y[t] != y_i:
                break
        X_t, y_t = X[t], y[t]
        X_t = aug1(image=_load_image(X_t))['image']
        c_t = _to_categorical(y_t, num_classes)
        r = np.random.uniform(0.5, 1.0)
        X_i = (X_i * r + X_t * (1 - r)).astype(np.float32)
        c_i = (c_i * r + c_t * (1 - r)).astype(np.float32)

    X_i = aug2(image=X_i)['image']
    return X_i, c_i


def _load_image(path):
    """画像の読み込み。"""
    with PIL.Image.open(path) as img:
        if img.mode != 'RGB':
            img = img.convert('RGB')
        return np.asarray(img, dtype=np.uint8)


def _to_categorical(index, num_classes):
    """indexからone-hot vectorを作成。(スカラー用)"""
    onehot = np.zeros((num_classes,), dtype=np.float32)
    onehot[index] = 1
    return onehot


def _create_autoaugment():
    """AutoAugment <https://arxiv.org/abs/1805.09501>

    元論文がPILベースでalbumentationsと挙動が合わなかったので、
    ほとんどの変換を実装しなおしていてalbumentationsを使った意味がほとんど無い感じになっている。
    (Rotateだけ面倒だったのでとりあえずalbumentationsを使っているけど、これも本来の挙動とは結構違う)
    """
    sp = {
        'ShearX': lambda p, mag: Affine(shear_x_mag=mag, p=p),
        'ShearY': lambda p, mag: Affine(shear_y_mag=mag, p=p),
        'TranslateX': lambda p, mag: Affine(translate_x_mag=mag, p=p),
        'TranslateY': lambda p, mag: Affine(translate_y_mag=mag, p=p),
        'Rotate': lambda p, mag: A.Rotate(limit=mag / 9 * 30, p=p),
        'Color': lambda p, mag: Color(mag=mag, p=p),
        'Posterize': lambda p, mag: Posterize(mag=mag, p=p),
        'Solarize': lambda p, mag: Solarize(mag=mag, p=p),
        'Contrast': lambda p, mag: Contrast(mag=mag, p=p),
        'Sharpness': lambda p, mag: Sharpness(mag=mag, p=p),
        'Brightness': lambda p, mag: Brightness(mag=mag, p=p),
        'AutoContrast': lambda p, mag: AutoContrast(p=p),
        'Equalize': lambda p, mag: Equalize(p=p),
        'Invert': lambda p, mag: A.InvertImg(p=p),
    }
    return A.OneOf([
        A.Compose([sp['Invert'](0.1, 7), sp['Contrast'](0.2, 6)]),
        A.Compose([sp['Rotate'](0.7, 2), sp['TranslateX'](0.3, 9)]),
        A.Compose([sp['Sharpness'](0.8, 1), sp['Sharpness'](0.9, 3)]),
        A.Compose([sp['ShearY'](0.5, 8), sp['TranslateY'](0.7, 9)]),
        A.Compose([sp['AutoContrast'](0.5, 8), sp['Equalize'](0.9, 2)]),
        A.Compose([sp['ShearY'](0.2, 7), sp['Posterize'](0.3, 7)]),
        A.Compose([sp['Color'](0.4, 3), sp['Brightness'](0.6, 7)]),
        A.Compose([sp['Sharpness'](0.3, 9), sp['Brightness'](0.7, 9)]),
        A.Compose([sp['Equalize'](0.6, 5), sp['Equalize'](0.5, 1)]),
        A.Compose([sp['Contrast'](0.6, 7), sp['Sharpness'](0.6, 5)]),
        A.Compose([sp['Color'](0.7, 7), sp['TranslateX'](0.5, 8)]),
        A.Compose([sp['Equalize'](0.3, 7), sp['AutoContrast'](0.4, 8)]),
        A.Compose([sp['TranslateY'](0.4, 3), sp['Sharpness'](0.2, 6)]),
        A.Compose([sp['Brightness'](0.9, 6), sp['Color'](0.2, 8)]),
        A.Compose([sp['Solarize'](0.5, 2), sp['Invert'](0.0, 3)]),
        A.Compose([sp['Equalize'](0.2, 0), sp['AutoContrast'](0.6, 0)]),
        A.Compose([sp['Equalize'](0.2, 8), sp['Equalize'](0.8, 4)]),
        A.Compose([sp['Color'](0.9, 9), sp['Equalize'](0.6, 6)]),
        A.Compose([sp['AutoContrast'](0.8, 4), sp['Solarize'](0.2, 8)]),
        A.Compose([sp['Brightness'](0.1, 3), sp['Color'](0.7, 0)]),
        A.Compose([sp['Solarize'](0.4, 5), sp['AutoContrast'](0.9, 3)]),
        A.Compose([sp['TranslateY'](0.9, 9), sp['TranslateY'](0.7, 9)]),
        A.Compose([sp['AutoContrast'](0.9, 2), sp['Solarize'](0.8, 3)]),
        A.Compose([sp['Equalize'](0.8, 8), sp['Invert'](0.1, 3)]),
        A.Compose([sp['TranslateY'](0.7, 9), sp['AutoContrast'](0.9, 1)]),
    ], p=1)


class Affine(A.ImageOnlyTransform):
    """Affine変換。"""

    def __init__(self, shear_x_mag=0, shear_y_mag=0, translate_x_mag=0, translate_y_mag=0, always_apply=False, p=.5):
        super().__init__(always_apply, p)
        self.shear_x_mag = shear_x_mag
        self.shear_y_mag = shear_y_mag
        self.translate_x_mag = translate_x_mag
        self.translate_y_mag = translate_y_mag

    def apply(self, img, **params):
        shear_x = self.shear_x_mag / 9 * 0.3 * np.random.choice([-1, 1])
        shear_y = self.shear_y_mag / 9 * 0.3 * np.random.choice([-1, 1])
        translate_x = self.translate_x_mag / 9 * (150 / 331) * np.random.choice([-1, 1])
        translate_y = self.translate_y_mag / 9 * (150 / 331) * np.random.choice([-1, 1])
        img = PIL.Image.fromarray(img, mode='RGB')
        data = (1, shear_x, translate_x, shear_y, 1, translate_y)
        return np.asarray(img.transform(img.size, PIL.Image.AFFINE, data, PIL.Image.BICUBIC, fillcolor=(128, 128, 128)), dtype=np.uint8)


class Color(A.ImageOnlyTransform):
    """PIL.ImageEnhance.ColorなTransform"""

    def __init__(self, mag=10, always_apply=False, p=.5):
        super().__init__(always_apply, p)
        self.mag = mag

    def apply(self, img, **params):
        factor = 1 + self.mag / 9 * np.random.choice([-1, 1])
        img = PIL.Image.fromarray(img, mode='RGB')
        return np.asarray(PIL.ImageEnhance.Color(img).enhance(factor), dtype=np.uint8)


class Posterize(A.ImageOnlyTransform):
    """PIL.ImageOps.posterizeなTransform"""

    def __init__(self, mag=10, always_apply=False, p=.5):
        super().__init__(always_apply, p)
        self.mag = mag

    def apply(self, img, **params):
        bit = np.round(8 - self.mag * 4 / 9).astype(np.int)
        img = PIL.Image.fromarray(img, mode='RGB')
        return np.asarray(PIL.ImageOps.posterize(img, bit), dtype=np.uint8)


class Solarize(A.ImageOnlyTransform):
    """PIL.ImageOps.solarizeなTransform"""

    def __init__(self, mag=10, always_apply=False, p=.5):
        super().__init__(always_apply, p)
        self.mag = mag

    def apply(self, img, **params):
        threshold = 256 - self.mag * 256 / 9
        img = PIL.Image.fromarray(img, mode='RGB')
        return np.asarray(PIL.ImageOps.solarize(img, threshold), dtype=np.uint8)


class Contrast(A.ImageOnlyTransform):
    """PIL.ImageEnhance.ContrastなTransform"""

    def __init__(self, mag=10, always_apply=False, p=.5):
        super().__init__(always_apply, p)
        self.mag = mag

    def apply(self, img, **params):
        factor = 1 + self.mag / 9 * np.random.choice([-1, 1])
        img = PIL.Image.fromarray(img, mode='RGB')
        return np.asarray(PIL.ImageEnhance.Contrast(img).enhance(factor), dtype=np.uint8)


class Sharpness(A.ImageOnlyTransform):
    """PIL.ImageEnhance.SharpnessなTransform"""

    def __init__(self, mag=10, always_apply=False, p=.5):
        super().__init__(always_apply, p)
        self.mag = mag

    def apply(self, img, **params):
        factor = 1 + self.mag / 9 * np.random.choice([-1, 1])
        img = PIL.Image.fromarray(img, mode='RGB')
        return np.asarray(PIL.ImageEnhance.Sharpness(img).enhance(factor), dtype=np.uint8)


class Brightness(A.ImageOnlyTransform):
    """PIL.ImageEnhance.BrightnessなTransform"""

    def __init__(self, mag=10, always_apply=False, p=.5):
        super().__init__(always_apply, p)
        self.mag = mag

    def apply(self, img, **params):
        factor = 1 + self.mag / 9 * np.random.choice([-1, 1])
        img = PIL.Image.fromarray(img, mode='RGB')
        return np.asarray(PIL.ImageEnhance.Brightness(img).enhance(factor), dtype=np.uint8)


class AutoContrast(A.ImageOnlyTransform):
    """PIL.ImageOps.autocontrastなTransform"""

    def apply(self, img, **params):
        img = PIL.Image.fromarray(img, mode='RGB')
        return np.asarray(PIL.ImageOps.autocontrast(img), dtype=np.uint8)


class Equalize(A.ImageOnlyTransform):
    """PIL.ImageOps.equalizeなTransform"""

    def apply(self, img, **params):
        img = PIL.Image.fromarray(img, mode='RGB')
        return np.asarray(PIL.ImageOps.equalize(img), dtype=np.uint8)


if __name__ == '__main__':
    _main()
