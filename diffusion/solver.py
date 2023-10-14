import os
import time
import numpy as np
import torch
import librosa
from logger.saver import Saver
from logger import utils
from torch import autocast
from torch.cuda.amp import GradScaler

def test(args, model, vocoder, loader_test, f0_extractor, quantizer, saver):
    print(' [*] testing...')
    model.eval()

    # losses
    test_loss = 0.

    # intialization
    num_batches = len(loader_test)
    rtf_all = []

    # run
    with torch.no_grad():
        for bidx, data in enumerate(loader_test):
            fn = data['name'][0]
            print('--------')
            print('{}/{} - {}'.format(bidx, num_batches, fn))
            
            if data['f0'][0] == -1:
                data['f0'] = None
            if data['volume'][0] == -1:
                data['volume'] = None
            if data['aug_shift'][0] == -1:
                data['aug_shift'] = None

            if quantizer is not None:
                data['units'] = quantizer(data['units'])
                
            # unpack data
            for k in data.keys():
                if type(data[k]) is torch.Tensor:
                    data[k] = data[k].to(args.device)

            print('>>', data['name'][0])

            # forward
            st_time = time.time()
            mel = model(
                data['units'],
                data['f0'],
                data['volume'],
                data['spk_id'],
                gt_spec=data['mel'],
                infer=True,
                infer_speedup=args.infer.speedup,
                method=args.infer.method,
                k_step=args.model.k_step_max,
                spk_emb=data['spk_emb'])
            
            if data['f0'] is None:
                f0 = f0_extractor.model(mel=mel, infer=True, return_hz_f0=True)
            else:
                f0 = data['f0']

            signal = vocoder.infer(mel, f0)
            ed_time = time.time()

            # RTF
            run_time = ed_time - st_time
            song_time = signal.shape[-1] / args.data.sampling_rate
            rtf = run_time / song_time
            print('RTF: {}  | {} / {}'.format(rtf, run_time, song_time))
            rtf_all.append(rtf)

            # loss
            for i in range(args.train.batch_size):
                loss = model(
                    data['units'],
                    data['f0'],
                    data['volume'],
                    data['spk_id'],
                    gt_spec=data['mel'],
                    infer=False,
                    k_step=args.model.k_step_max,
                    spk_emb=data['spk_emb'])
                test_loss += loss.item()

            # log mel
            saver.log_spec(data['name'][0], data['mel'], mel)

            # log audio
            path_audio = os.path.join(args.data.valid_path, 'audio', data['name_ext'][0])
            audio, sr = librosa.load(path_audio, sr=args.data.sampling_rate)
            if len(audio.shape) > 1:
                audio = librosa.to_mono(audio)
            audio = torch.from_numpy(audio).unsqueeze(0).to(signal)
            saver.log_audio({fn + '/gt.wav': audio, fn + '/pred.wav': signal})

    # report
    test_loss /= args.train.batch_size
    test_loss /= num_batches

    # check
    print(' [test_loss] test_loss:', test_loss)
    print(' Real Time Factor', np.mean(rtf_all))
    return test_loss


def train(args, initial_global_step, model, optimizer, scheduler, vocoder, loader_train, loader_test):
    # saver
    saver = Saver(args, initial_global_step=initial_global_step)

    # model size
    params_count = utils.get_network_paras_amount({'model': model})
    saver.log_info('--- model size ---')
    saver.log_info(params_count)

    if args.data.is_tts:
        from encoder.fcpe.model import FCPEInfer
        f0_extractor = FCPEInfer(model_path='pretrain/fcpe/fcpe.pt')
    else:
        f0_extractor = None
    
    if args.train.use_units_quantize:
        saver.log_info("load quantizer")
        if args.train.units_quantize_type == "kmeans":
            from quantize.kmeans_codebook import EuclideanCodebook
            from cluster import get_cluster_model
            codebook_weight = get_cluster_model(args.model.text2semantic.codebook_path).__dict__["cluster_centers_"]
            quantizer = EuclideanCodebook(codebook_weight).to(args.device)
        else:
            raise ValueError(' [x] Unknown quantize_type: ' + args.train.units_quantize_type)
    else:
        quantizer = None

    # run
    num_batches = len(loader_train)
    start_epoch = initial_global_step // num_batches
    model.train()
    saver.log_info('======= start training =======')
    scaler = GradScaler()
    if args.train.amp_dtype == 'fp32':
        dtype = torch.float32
    elif args.train.amp_dtype == 'fp16':
        dtype = torch.float16
    elif args.train.amp_dtype == 'bf16':
        dtype = torch.bfloat16
    else:
        raise ValueError(' [x] Unknown amp_dtype: ' + args.train.amp_dtype)
    for epoch in range(start_epoch, args.train.epochs):
        for batch_idx, data in enumerate(loader_train):
            if data['f0'][0] == -1:
                data['f0'] = None
            if data['volume'][0] == -1:
                data['volume'] = None
            if data['aug_shift'][0] == -1:
                data['aug_shift'] = None


            saver.global_step_increment()
            optimizer.zero_grad()

            # unpack data
            for k in data.keys():
                if type(data[k]) is torch.Tensor:
                    data[k] = data[k].to(args.device)

            if quantizer is not None:
                data['units'] = quantizer(data['units'])

            # forward
            if dtype == torch.float32:
                loss = model(data['units'].float(), data['f0'], data['volume'], data['spk_id'],
                             aug_shift=data['aug_shift'], gt_spec=data['mel'].float(), infer=False, k_step=args.model.k_step_max,
                             spk_emb=data['spk_emb'])
            else:
                with autocast(device_type=args.device, dtype=dtype):
                    loss = model(data['units'], data['f0'], data['volume'], data['spk_id'],
                                 aug_shift=data['aug_shift'], gt_spec=data['mel'], infer=False, k_step=args.model.k_step_max,
                                 spk_emb=data['spk_emb'])

            # handle nan loss
            if torch.isnan(loss):
                raise ValueError(' [x] nan loss ')
            else:
                # backpropagate
                if dtype == torch.float32:
                    loss.backward()
                    optimizer.step()
                else:
                    scaler.scale(loss).backward()
                    scaler.step(optimizer)
                    scaler.update()
                scheduler.step()

            # log loss
            if saver.global_step % args.train.interval_log == 0:
                current_lr = optimizer.param_groups[0]['lr']
                saver.log_info(
                    'epoch: {} | {:3d}/{:3d} | {} | batch/s: {:.2f} | lr: {:.6} | loss: {:.3f} | time: {} | step: {}'.format(
                        epoch,
                        batch_idx,
                        num_batches,
                        args.env.expdir,
                        args.train.interval_log / saver.get_interval_time(),
                        current_lr,
                        loss.item(),
                        saver.get_total_time(),
                        saver.global_step
                    )
                )

                saver.log_value({
                    'train/loss': loss.item()
                })

                saver.log_value({
                    'train/lr': current_lr
                })

            # validation
            if saver.global_step % args.train.interval_val == 0:
                optimizer_save = optimizer if args.train.save_opt else None

                # save latest
                saver.save_model(model, optimizer_save, postfix=f'{saver.global_step}')
                last_val_step = saver.global_step - args.train.interval_val
                if last_val_step % args.train.interval_force_save != 0:
                    saver.delete_model(postfix=f'{last_val_step}')

                # run testing set
                test_loss = test(args, model, vocoder, loader_test, f0_extractor, quantizer, saver)
                
                # log loss
                saver.log_info(
                    ' --- <validation> --- \nloss: {:.3f}. '.format(
                        test_loss,
                    )
                )

                saver.log_value({
                    'validation/loss': test_loss
                })

                model.train()
