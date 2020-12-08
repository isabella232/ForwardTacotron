import time
from typing import Tuple

import torch
from torch.optim.optimizer import Optimizer
from torch.utils.data.dataset import Dataset
from torch.utils.tensorboard import SummaryWriter

from models.forward_tacotron import ForwardTacotron
from trainer.common import Averager, TTSSession, MaskedL1, MaskedL2
from utils import hparams as hp
from utils.checkpoints import save_checkpoint
from utils.dataset import get_tts_datasets
from utils.decorators import ignore_exception
from utils.display import stream, simple_table, plot_mel, plot_pitch
from utils.dsp import reconstruct_waveform, np_now
from utils.paths import Paths


class ForwardTrainer:

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.writer = SummaryWriter(log_dir=paths.forward_log, comment='v1')
        self.l1_loss = MaskedL1()
        self.l2_loss = MaskedL2()

    def train(self, model: ForwardTacotron, optimizer: Optimizer) -> None:
        for i, session_params in enumerate(hp.forward_schedule, 1):
            lr, max_step, bs = session_params
            if model.get_step() < max_step:
                train_set, val_set = get_tts_datasets(
                    path=self.paths.data, batch_size=bs, r=1, model_type='forward')
                session = TTSSession(
                    index=i, r=1, lr=lr, max_step=max_step,
                    bs=bs, train_set=train_set, val_set=val_set)
                self.train_session(model, optimizer, session)

    def train_session(self, model: ForwardTacotron,
                      optimizer: Optimizer, session: TTSSession) -> None:
        current_step = model.get_step()
        training_steps = session.max_step - current_step
        total_iters = len(session.train_set)
        epochs = training_steps // total_iters + 1
        simple_table([(f'Steps', str(training_steps // 1000) + 'k Steps'),
                      ('Batch Size', session.bs),
                      ('Learning Rate', session.lr)])

        for g in optimizer.param_groups:
            g['lr'] = session.lr

        m_loss_avg = Averager()
        dur_loss_avg = Averager()
        sil_loss_avg = Averager()
        duration_avg = Averager()
        pitch_loss_avg = Averager()
        device = next(model.parameters()).device  # use same device as model parameters
        for e in range(1, epochs + 1):

            duration_tensors = []
            duration_tensors_target = []
            for i, (x, m, ids, x_lens, mel_lens, dur, pitch, dur_sil) in enumerate(session.train_set, 1):

                start = time.time()
                model.train()
                x, m, dur, x_lens, mel_lens, pitch, dur_sil = x.to(device), m.to(device), dur.to(device),\
                                                     x_lens.to(device), mel_lens.to(device), pitch.to(device), dur_sil.to(device)

                m1_hat, m2_hat, dur_hat, pitch_hat, sil_hat = model(x, m, dur, mel_lens, pitch, dur_sil)

                m1_loss = self.l1_loss(m1_hat, m, mel_lens)
                m2_loss = self.l1_loss(m2_hat, m, mel_lens)

                duration_tensors.append(dur_hat.flatten())
                duration_tensors_target.append(dur.flatten())
                sil_loss = self.l1_loss(sil_hat.unsqueeze(1), dur_sil.unsqueeze(1), x_lens)
                pitch_loss = self.l1_loss(pitch_hat, pitch.unsqueeze(1), x_lens)

                dur_loss = self.l1_loss(dur_hat.unsqueeze(1), dur.unsqueeze(1), x_lens)

                loss = m1_loss + m2_loss + 0.1 * dur_loss + 0.1 * pitch_loss + 0.1 * sil_loss
                optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), hp.tts_clip_grad_norm)
                optimizer.step()
                m_loss_avg.add(m1_loss.item() + m2_loss.item())
                dur_loss_avg.add(dur_loss.item())
                sil_loss_avg.add(sil_loss.item())
                step = model.get_step()
                k = step // 1000

                duration_avg.add(time.time() - start)
                pitch_loss_avg.add(pitch_loss.item())

                speed = 1. / duration_avg.get()
                msg = f'| Epoch: {e}/{epochs} ({i}/{total_iters}) | Mel Loss: {m_loss_avg.get():#.4} ' \
                      f'| Dur Loss: {dur_loss_avg.get():#.4}| Sil Loss: {sil_loss_avg.get():#.4} | Pitch Loss: {pitch_loss_avg.get():#.4} ' \
                      f'| {speed:#.2} steps/s | Step: {k}k | '

                if step % hp.forward_checkpoint_every == 0:
                    ckpt_name = f'forward_step{k}K'
                    save_checkpoint('forward', self.paths, model, optimizer,
                                    name=ckpt_name, is_silent=True)

                if step % hp.forward_plot_every == 0:
                    self.generate_plots(model, session)

                self.writer.add_scalar('Mel_Loss/train', m1_loss + m2_loss, model.get_step())
                self.writer.add_scalar('Pitch_Loss/train', pitch_loss, model.get_step())
                self.writer.add_scalar('Duration_Loss/train', dur_loss, model.get_step())
                self.writer.add_scalar('Silence_Loss/train', sil_loss, model.get_step())
                self.writer.add_scalar('Params/batch_size', session.bs, model.get_step())
                self.writer.add_scalar('Params/learning_rate', session.lr, model.get_step())

                stream(msg)
            duration_concat = torch.cat(duration_tensors, dim=0)
            duration_concat_target = torch.cat(duration_tensors_target, dim=0)

            m_val_loss, dur_val_loss, pitch_val_loss, sil_val_loss, duration_concat_val, duration_concat_val_target = self.evaluate(model, session.val_set)
            self.writer.add_scalar('Mel_Loss/val', m_val_loss, model.get_step())
            self.writer.add_scalar('Duration_Loss/val', dur_val_loss, model.get_step())
            self.writer.add_scalar('Silence_Loss/val', sil_val_loss, model.get_step())
            self.writer.add_scalar('Pitch_Loss/val', pitch_val_loss, model.get_step())
            self.writer.add_histogram('Duration_Histo/train', duration_concat, model.get_step())
            self.writer.add_histogram('Duration_Histo/train_target', duration_concat_target, model.get_step())
            self.writer.add_histogram('Duration_Histo/val', duration_concat_val, model.get_step())
            self.writer.add_histogram('Duration_Histo/val_target', duration_concat_val_target, model.get_step())
            save_checkpoint('forward', self.paths, model, optimizer, is_silent=True)

            m_loss_avg.reset()
            duration_avg.reset()
            pitch_loss_avg.reset()
            dur_loss_avg.reset()
            sil_loss_avg.reset()
            print(' ')

    def evaluate(self, model: ForwardTacotron, val_set: Dataset) -> Tuple[float, float, float, float, torch.tensor, torch.tensor]:
        model.eval()
        m_val_loss = 0
        dur_val_loss = 0
        sil_val_loss = 0
        pitch_val_loss = 0
        device = next(model.parameters()).device
        duration_tensors = []
        duration_tensors_target = []
        for i, (x, m, ids, x_lens, mel_lens, dur, pitch, dur_sil) in enumerate(val_set, 1):
            x, m, dur, x_lens, mel_lens, pitch, dur_sil = x.to(device), m.to(device), dur.to(device), \
                                                 x_lens.to(device), mel_lens.to(device), pitch.to(device), dur_sil.to(device)
            with torch.no_grad():
                m1_hat, m2_hat, dur_hat, pitch_hat, sil_hat = model(x, m, dur, mel_lens, pitch, dur_sil)
                m1_loss = self.l1_loss(m1_hat, m, mel_lens)
                m2_loss = self.l1_loss(m2_hat, m, mel_lens)
                dur_loss = self.l1_loss(dur_hat.unsqueeze(1), dur.unsqueeze(1), x_lens)
                sil_loss = self.l1_loss(sil_hat.unsqueeze(1), dur_sil.unsqueeze(1), x_lens)
                pitch_val_loss += self.l1_loss(pitch_hat, pitch.unsqueeze(1), x_lens)
                m_val_loss += m1_loss.item() + m2_loss.item()
                dur_val_loss += dur_loss.item()
                sil_val_loss += sil_loss.item()
                duration_tensors.append(dur_hat.flatten())
                duration_tensors_target.append(dur.flatten())
        m_val_loss /= len(val_set)
        dur_val_loss /= len(val_set)
        pitch_val_loss /= len(val_set)
        sil_val_loss /= len(val_set)
        return m_val_loss, dur_val_loss, pitch_val_loss, sil_val_loss, torch.cat(duration_tensors, dim=0), torch.cat(duration_tensors_target, dim=0)

    @ignore_exception
    def generate_plots(self, model: ForwardTacotron, session: TTSSession) -> None:
        model.eval()
        device = next(model.parameters()).device
        x, m, ids, x_lens, mel_lens, dur, pitch, sil = session.val_sample
        x, m, dur, mel_lens, pitch, sil = x.to(device), m.to(device), dur.to(device), \
                                          mel_lens.to(device), pitch.to(device), sil.to(device)

        m1_hat, m2_hat, dur_hat, pitch_hat, sil_hat = model(x, m, dur, mel_lens, pitch, sil)
        m1_hat = np_now(m1_hat)[0, :600, :]
        m2_hat = np_now(m2_hat)[0, :600, :]
        m = np_now(m)[0, :600, :]

        m1_hat_fig = plot_mel(m1_hat)
        m2_hat_fig = plot_mel(m2_hat)
        m_fig = plot_mel(m)

        sil_fig = plot_pitch(np_now(sil[0]))
        sil_gta_fig = plot_pitch(np_now(sil_hat[0]))
        pitch_fig = plot_pitch(np_now(pitch[0]))
        pitch_gta_fig = plot_pitch(np_now(pitch_hat.squeeze()[0]))

        self.writer.add_figure('Silence/target', sil_fig, model.step)
        self.writer.add_figure('Silence/ground_truth_aligned', sil_gta_fig, model.step)
        self.writer.add_figure('Pitch/target', pitch_fig, model.step)
        self.writer.add_figure('Pitch/ground_truth_aligned', pitch_gta_fig, model.step)
        self.writer.add_figure('Ground_Truth_Aligned/target', m_fig, model.step)
        self.writer.add_figure('Ground_Truth_Aligned/linear', m1_hat_fig, model.step)
        self.writer.add_figure('Ground_Truth_Aligned/postnet', m2_hat_fig, model.step)

        m2_hat_wav = reconstruct_waveform(m2_hat)
        target_wav = reconstruct_waveform(m)

        self.writer.add_audio(
            tag='Ground_Truth_Aligned/target_wav', snd_tensor=target_wav,
            global_step=model.step, sample_rate=hp.sample_rate)
        self.writer.add_audio(
            tag='Ground_Truth_Aligned/postnet_wav', snd_tensor=m2_hat_wav,
            global_step=model.step, sample_rate=hp.sample_rate)

        m1_hat, m2_hat, dur_hat, pitch_hat, sil_hat = model.generate(x[0, :x_lens[0]].tolist())
        m1_hat_fig = plot_mel(m1_hat)
        m2_hat_fig = plot_mel(m2_hat)

        pitch_gen_fig = plot_pitch(np_now(pitch_hat.squeeze()))
        sil_gen_fig = plot_pitch(np_now(sil_hat.squeeze()))

        self.writer.add_figure('Pitch/generated', pitch_gen_fig, model.step)
        self.writer.add_figure('Silence/generated', sil_gen_fig, model.step)

        self.writer.add_figure('Generated/target', m_fig, model.step)
        self.writer.add_figure('Generated/linear', m1_hat_fig, model.step)
        self.writer.add_figure('Generated/postnet', m2_hat_fig, model.step)

        m2_hat_wav = reconstruct_waveform(m2_hat)

        self.writer.add_audio(
            tag='Generated/target_wav', snd_tensor=target_wav,
            global_step=model.step, sample_rate=hp.sample_rate)
        self.writer.add_audio(
            tag='Generated/postnet_wav', snd_tensor=m2_hat_wav,
            global_step=model.step, sample_rate=hp.sample_rate)
